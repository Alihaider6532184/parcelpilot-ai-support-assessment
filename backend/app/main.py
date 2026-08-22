from __future__ import annotations
import json, re, uuid
from datetime import date
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
import jwt
from pydantic import ValidationError
from .config import SESSION_SECRET, FRONTEND_ORIGIN, GROQ_API_KEY, GEMINI_API_KEY, GROQ_MODEL, GEMINI_MODEL, MODEL_DAILY_LIMIT
from .schemas.models import *
from .services.runtime import Runtime
from .agent.providers import Provider, SYSTEM, TOOLS
from .agent.local import ToolSelection, high_confidence_selection, infer_fallback
from .reliability.rules import evaluate

app=FastAPI(title="ParcelPilot AI Support", version="1.0.0")
app.add_middleware(CORSMiddleware,allow_origins=[FRONTEND_ORIGIN,"http://localhost:3000"],allow_credentials=True,allow_methods=["*"],allow_headers=["*"])
runtime: Runtime|None=None; provider=Provider(GROQ_API_KEY,GEMINI_API_KEY,GROQ_MODEL,GEMINI_MODEL)
model_calls_today=0; model_call_day=date.today()
# This is deliberately server-side, minimal context for conversational phrases
# such as "escalate this". Every follow-up re-fetches the record through the
# scoped repository, and logout removes the context.
LAST_CONTEXT: dict[str, dict[str, str]] = {}
USERS={"priya":{"user_id":"priya","role":"support_agent","allowed_account_ids":["ACCT-001"],"all_accounts":False},"arjun":{"user_id":"arjun","role":"support_agent","allowed_account_ids":["ACCT-002"],"all_accounts":False},"manager":{"user_id":"manager","role":"ops_manager","allowed_account_ids":[],"all_accounts":True},"viewer":{"user_id":"viewer","role":"viewer","allowed_account_ids":["ACCT-003"],"all_accounts":False}}

@app.on_event("startup")
def startup():
    global runtime; runtime=Runtime()
def get_session(request: Request)->Session:
    token=request.cookies.get("parcelpilot_session")
    if not token: raise HTTPException(status_code=401,detail="Sign in with a demo role")
    try: return Session(**jwt.decode(token,SESSION_SECRET,algorithms=["HS256"]))
    except Exception: raise HTTPException(status_code=401,detail="Invalid session")
def rt()->Runtime:
    if runtime is None: raise HTTPException(status_code=503,detail="Service is warming up")
    return runtime

@app.get("/healthz")
def health(): return {"status":"ok","ready":runtime is not None}
@app.get("/api/users")
def users(): return [{"user_id":u["user_id"],"role":u["role"],"allowed_account_ids":u["allowed_account_ids"],"all_accounts":u["all_accounts"]} for u in USERS.values()]
@app.post("/api/auth/login")
def login(q: LoginRequest, response: Response):
    u=USERS.get(q.user_id)
    if not u: raise HTTPException(status_code=400,detail="Unknown demo user")
    production_cookie = FRONTEND_ORIGIN.startswith("https://")
    payload={**u,"session_id":str(uuid.uuid4())}
    response.set_cookie("parcelpilot_session",jwt.encode(payload,SESSION_SECRET,algorithm="HS256"),httponly=True,samesite="none" if production_cookie else "lax",secure=production_cookie,max_age=3600)
    return u
@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    try:
        session=Session(**jwt.decode(request.cookies.get("parcelpilot_session", ""),SESSION_SECRET,algorithms=["HS256"]))
        LAST_CONTEXT.pop(_context_key(session), None)
    except Exception:
        pass
    response.delete_cookie("parcelpilot_session"); return {"ok":True}
@app.get("/api/me")
def me(s: Session=Depends(get_session)): return s

def _valid_id(value, prefix):
    value=str(value or "").upper()
    return value if re.fullmatch(fr"{prefix}-\d+",value) else None


def execute_tool(name, args, s, r, context=None):
    if name=="lookup_records": return r.repo.lookup(LookupQuery(**args),s)
    if name=="search_documents": return r.documents.search(DocumentQuery(**args),s)
    if name=="evaluate_entitlement":
        q=EvaluateQuery(**args); lookup=r.repo.order(q.order_id,s); account=lookup["related"]["account"]
        observed=q.reported_pickup_at or lookup["record"]["fields"].get("pickup_actual_at")
        return evaluate(lookup["record"]["fields"],account,[{"citation_id":d["id"],"text":d["text"],**d["metadata"]} for d in r.docs],q.evaluation_type,observed,r.dataset_now)
    if name=="analyze_operations": return r.analytics.analyze(AnalyticsQuery(**args),s)
    if name=="propose_escalation":
        # Classify first, then deny by role. This intentionally happens before
        # ID/argument validation so a viewer always receives the real reason.
        if s.role == "viewer": raise HTTPException(status_code=403,detail="This role is not permitted to create escalation proposals")
        context=context or {}
        order_id=_valid_id(args.get("order_id"),"ORD") or context.get("order_id")
        ticket_id=_valid_id(args.get("ticket_id"),"TKT") or context.get("ticket_id")
        account_id=_valid_id(args.get("account_id"),"ACCT") or context.get("account_id")
        if order_id:
            lookup=r.repo.order(order_id,s); account_id=lookup["record"]["fields"]["account_id"]
        elif ticket_id:
            lookup=r.repo.lookup(LookupQuery(record_type="ticket",record_id=ticket_id),s); account_id=lookup["record"]["fields"]["account_id"]
        elif account_id:
            r.repo.account(account_id,s)
        else:
            raise HTTPException(status_code=422,detail="An authorized order, ticket, or account ID is required before an escalation can be proposed")
        q=ProposalQuery(account_id=account_id,order_id=order_id,ticket_id=ticket_id,reason=args.get("reason") or "Escalation requested",severity=args.get("severity") or "P2",evidence_citation_ids=args.get("evidence_citation_ids") or [])
        return r.actions.propose(q,s)
    raise HTTPException(status_code=400,detail="Unknown tool")

def _remember_context(session: Session, record: dict) -> None:
    context={key: record[key] for key in ("account_id", "order_id", "ticket_id") if record.get(key)}
    if context:
        LAST_CONTEXT[_context_key(session)]=context


def _context_key(session: Session) -> str:
    return session.session_id or f"test:{session.user_id}"


def _answer_for(name, result, r):
    if name=="propose_escalation":
        return f"I prepared an escalation draft for {result['payload_preview']['account_id']}. It is pending your explicit confirmation; no action has been executed."
    if name=="analyze_operations":
        if result["recurring_issues"]:
            groups="; ".join(f"{x['label']} ({x['account_count']} accounts; tickets {', '.join(x['ticket_ids'])})" for x in result["recurring_issues"])
            return f"I analyzed {result['ticket_count']} real tickets across {len(result['accounts_analyzed'])} accounts. Recurring cross-customer issues: {groups}. Dataset snapshot: {result['dataset_now']}."
        repeats="; ".join(f"{x['label']} ({', '.join(x['ticket_ids'])}, one customer only)" for x in result["same_customer_repeats"])
        note=f" Same-customer repeats found: {repeats}." if repeats else ""
        return f"I analyzed {result['ticket_count']} real tickets across {len(result['accounts_analyzed'])} accounts. No significant recurring issue was found across {result['minimum_accounts']} or more customers.{note} Dataset snapshot: {result['dataset_now']}."
    if name=="evaluate_entitlement":
        if result["evaluation_type"]=="cancellation":
            fee_note="This is not fee-free." if result["fee_inr"] else "This is fee-free."
            return f"For {result['order_id']}, the deterministic cancellation evaluation is **{result['result']}**. Cancellation fee: INR {result['fee_inr']}. {fee_note} {result['recommended_next_step']}. Governing citations: {', '.join(result['governing_sources'])}."
        if result["result"]=="needs_verification":
            return f"For {result['order_id']}, the service-credit calculation needs verification. Missing facts: {', '.join(result['missing_or_conflicting_facts'])}. Governing citations: {', '.join(result['governing_sources'])}."
        return f"For {result['order_id']}, the deterministic service-credit evaluation is **{result['result']}**. Credit: INR {result['credit_inr']}. {result['recommended_next_step']}. Governing citations: {', '.join(result['governing_sources'])}."
    if name=="lookup_records":
        records=result.get("records")
        if records is not None:
            if not records: return f"No authorized {result['record']['record_type']} records were found. Dataset snapshot: {r.dataset_now}."
            labels=[x.get("order_id") or x.get("ticket_id") or f"{x.get('account_id')} ({x.get('account_name','account')})" for x in records]
            return f"Found {len(records)} authorized {result['record']['record_type']} records: {', '.join(labels)}. Dataset snapshot: {r.dataset_now}."
        rec=result["record"]["fields"]; label=rec.get("order_id") or rec.get("ticket_id") or rec.get("account_id") or "record"
        details="; ".join(f"{k.replace('_',' ')}: {v}" for k,v in rec.items() if v not in (None,""))
        return f"Authorized details for {label}: {details}. Dataset snapshot: {r.dataset_now}."
    return "I found the following authoritative passages. I excluded deprecated policy and unverified ticket history from the current answer: "+" ".join(x["text"] for x in result.get("results",[])[:3])


def _dispatch(selection: ToolSelection, message, s, r, source):
    event={"type":"tool","name":selection.name,"status":"running","selection_source":source}
    try:
        result=execute_tool(selection.name,selection.arguments,s,r,LAST_CONTEXT.get(_context_key(s)))
        event.update(status="complete",result=result)
        if selection.name=="lookup_records" and result.get("record",{}).get("fields"): _remember_context(s,result["record"]["fields"])
        return {"answer":_answer_for(selection.name,result,r),"events":[event],"dataset_now":r.dataset_now,"model_routing":source}
    except HTTPException as exc:
        status="denied" if exc.status_code==403 else "needs_input" if exc.status_code==422 else "error"
        event.update(status=status,error={"status_code":exc.status_code,"detail":exc.detail})
        prefix="Access denied" if exc.status_code==403 else "I need more information" if exc.status_code==422 else "The requested tool could not complete"
        return {"answer":f"{prefix}: {exc.detail}.","events":[event],"dataset_now":r.dataset_now,"model_routing":source}
    except (ValidationError,ValueError,TypeError,KeyError):
        event.update(status="error",error={"status_code":422,"detail":"Invalid tool arguments"})
        return {"answer":"The selected tool needs valid record details before it can run.","events":[event],"dataset_now":r.dataset_now,"model_routing":source}


async def _model_selection(message: str, session: Session, context: dict, router_provider=None):
    global model_calls_today, model_call_day
    today=date.today()
    if today != model_call_day: model_call_day, model_calls_today = today, 0
    selected_provider=router_provider or provider
    if router_provider is None and (not (GROQ_API_KEY or GEMINI_API_KEY) or model_calls_today >= MODEL_DAILY_LIMIT): return None
    try:
        model_calls_today += 1
        scope={"role":session.role,"allowed_account_ids":session.allowed_account_ids,"all_accounts":session.all_accounts,"current_record_context":context or None}
        msg=await selected_provider.complete([{"role":"system","content":SYSTEM+"\nAuthenticated session context: "+json.dumps(scope)},{"role":"user","content":message}],TOOLS,tool_choice="required")
        if not msg: return None
        calls=msg.get("tool_calls") or []
        if not calls: return None
        fn=calls[0].get("function",{}); name=fn.get("name"); raw=fn.get("arguments",{})
        args=json.loads(raw) if isinstance(raw,str) else raw
        if name not in {"lookup_records","search_documents","evaluate_entitlement","analyze_operations","propose_escalation"}: return None
        return ToolSelection(name,args or {}),"native:"+msg.get("_provider","model")
    except Exception:
        return None


async def run_agent(message, s, r, router_provider=None):
    context=LAST_CONTEXT.get(_context_key(s),{})
    model_choice=await _model_selection(message,s,context,router_provider)
    guard=high_confidence_selection(message,context)
    if model_choice:
        selection,source=model_choice
        if guard and selection.name != guard.name:
            selection=guard; source+="+safety_correction"
        elif guard:
            selection=ToolSelection(selection.name,{**selection.arguments,**guard.arguments})
    else:
        selection=infer_fallback(message,context); source="local_quota_fallback"
    return _dispatch(selection,message,s,r,source)


def local_answer(message,s,r):
    """Synchronous deterministic fallback retained for offline tests/operation."""
    context=LAST_CONTEXT.get(_context_key(s),{})
    return _dispatch(infer_fallback(message,context),message,s,r,"local_quota_fallback")

@app.post("/api/chat")
async def chat(q: ChatRequest, response: Response, s: Session=Depends(get_session), r: Runtime=Depends(rt)):
    result=await run_agent(q.message,s,r)
    # Make it explicit to every intermediary that each POST is a new agent turn.
    response.headers["Cache-Control"]="no-store"
    result["turn_id"]=q.turn_id or str(uuid.uuid4()); return result
@app.post("/api/actions/{proposal_id}/confirm")
def confirm(proposal_id: str, q: ConfirmRequest, s: Session=Depends(get_session), r: Runtime=Depends(rt)): return r.actions.confirm(proposal_id,q.confirmed,s)
