from __future__ import annotations
import json, re, uuid
from datetime import date
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
import jwt
from .config import SESSION_SECRET, FRONTEND_ORIGIN, GROQ_API_KEY, GEMINI_API_KEY, GROQ_MODEL, GEMINI_MODEL, MODEL_DAILY_LIMIT
from .schemas.models import *
from .services.runtime import Runtime
from .agent.providers import Provider, SYSTEM, TOOLS
from .agent.local import infer
from .reliability.rules import evaluate

app=FastAPI(title="ParcelPilot AI Support", version="1.0.0")
app.add_middleware(CORSMiddleware,allow_origins=[FRONTEND_ORIGIN,"http://localhost:3000"],allow_credentials=True,allow_methods=["*"],allow_headers=["*"])
runtime: Runtime|None=None; provider=Provider(GROQ_API_KEY,GEMINI_API_KEY,GROQ_MODEL,GEMINI_MODEL)
model_calls_today=0; model_call_day=date.today()
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
    response.set_cookie("parcelpilot_session",jwt.encode(u,SESSION_SECRET,algorithm="HS256"),httponly=True,samesite="none" if production_cookie else "lax",secure=production_cookie,max_age=3600)
    return u
@app.post("/api/auth/logout")
def logout(response: Response): response.delete_cookie("parcelpilot_session"); return {"ok":True}
@app.get("/api/me")
def me(s: Session=Depends(get_session)): return s

def execute_tool(name, args, s, r):
    if name=="lookup_records": return r.repo.lookup(LookupQuery(**args),s)
    if name=="search_documents": return r.documents.search(DocumentQuery(**args),s)
    if name=="evaluate_entitlement":
        q=EvaluateQuery(**args); lookup=r.repo.order(q.order_id,s); account=lookup["related"]["account"]
        return evaluate(lookup["record"]["fields"],account,[{"citation_id":d["id"],"text":d["text"],**d["metadata"]} for d in r.docs],q.evaluation_type,q.reported_pickup_at,r.dataset_now)
    if name=="propose_escalation": return r.actions.propose(ProposalQuery(**args),s)
    raise HTTPException(status_code=400,detail="Unknown tool")

def local_answer(message, s, r):
    events=[]; calls=infer(message); first_name, first_q=calls[0]; events.append({"type":"tool","name":first_name,"status":"running"}); first=execute_tool(first_name,first_q.model_dump(),s,r); events[-1]["status"]="complete"; events[-1]["result"]=first
    if first_name=="lookup_records":
        rec=first["record"]["fields"]; low=message.lower(); oid=rec.get("order_id");
        if any(k in low for k in ("escalate", "create follow-up", "urgent")):
            account_id=rec.get("account_id")
            proposal=execute_tool("propose_escalation", {"account_id":account_id,"order_id":oid,"ticket_id":rec.get("ticket_id"),"reason":message,"severity":"P1" if "urgent" in low else "P2","evidence_citation_ids":[]},s,r)
            events.append({"type":"tool","name":"propose_escalation","status":"complete","result":proposal})
            return {"answer":f"I prepared an escalation draft for {account_id}. It is pending your explicit confirmation; no action has been executed.","events":events,"dataset_now":r.dataset_now}
        if oid and any(k in low for k in ("cancel","fee")):
            events.append({"type":"tool","name":"search_documents","status":"running"}); d=r.documents.search(DocumentQuery(query="cancellation fee BOOKED pickup agreement SOP",account_id=rec.get("account_id")),s); events[-1]["status"]="complete"; events[-1]["result"]=d
            events.append({"type":"tool","name":"evaluate_entitlement","status":"running"}); ev=execute_tool("evaluate_entitlement",{"order_id":oid,"evaluation_type":"cancellation","reported_pickup_at":None},s,r); events[-1]["status"]="complete"; events[-1]["result"]=ev
            fee_note = "This is not fee-free." if ev["fee_inr"] else "This is fee-free."
            answer=f"For {oid}, the deterministic evaluation is **{ev['result']}**. Cancellation fee: INR {ev['fee_inr']}. {fee_note} {ev['recommended_next_step']}. Governing citations: {', '.join(ev['governing_sources'])}."
        elif oid and any(k in low for k in ("credit","late","pickup","carrier")):
            events.append({"type":"tool","name":"search_documents","status":"running"}); d=r.documents.search(DocumentQuery(query="failed pickup service credit carrier fault threshold",account_id=rec.get("account_id")),s); events[-1]["status"]="complete"; events[-1]["result"]=d
            answer=f"I found {oid} for account {rec.get('account_id')}. A pickup-time observation is required before promising a credit. The current evidence is scoped to the account and the SOP/agreement citations are shown in the tool results."
        else:
            label = rec.get("order_id") or rec.get("ticket_id") or rec.get("account_id") or "record"
            details = "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in rec.items() if v not in (None, ""))
            answer = f"Authorized details for {label}: {details}. Dataset snapshot: {r.dataset_now}."
    else:
        answer="I found the following authoritative passages. I excluded deprecated policy and unverified ticket history from the current answer: " + " ".join(x["text"] for x in first.get("results",[])[:3])
    return {"answer":answer,"events":events,"dataset_now":r.dataset_now}

async def hosted_route(message: str, session: Session, r: Runtime):
    """Use a free-tier model for tool selection, never for authorization/decisions.

    The deterministic planner remains the safe answer path. This bounded probe
    makes Groq/Gemini tool calling real when a key is configured, while a quota
    error transparently falls back to the same tested local path.
    """
    global model_calls_today, model_call_day
    today=date.today()
    if today != model_call_day: model_call_day, model_calls_today = today, 0
    if not (GROQ_API_KEY or GEMINI_API_KEY) or model_calls_today >= MODEL_DAILY_LIMIT: return None
    try:
        model_calls_today += 1
        msg=await provider.complete([{"role":"system","content":SYSTEM},{"role":"user","content":message}],TOOLS)
        if not msg: return None
        calls=msg.get("tool_calls") or []
        routed=[]
        for call in calls[:2]:
            fn=call.get("function",{}); name=fn.get("name"); raw=fn.get("arguments",{})
            try: args=json.loads(raw) if isinstance(raw,str) else raw
            except Exception: continue
            if name in {"lookup_records","search_documents","evaluate_entitlement","propose_escalation"}:
                # Execute only through the same guarded dispatcher used offline.
                if name == "propose_escalation":
                    # The deterministic path owns proposal creation so a model
                    # retry can never create duplicate pending actions.
                    routed.append({"type":"tool","name":name,"status":"model_selected_pending_safe_dispatch"})
                else:
                    result=execute_tool(name,args,session,r)
                    routed.append({"type":"tool","name":name,"status":"model_selected_and_guarded","result":result})
        return routed or [{"type":"model","name":"provider_router","status":"completed"}]
    except Exception:
        return None

@app.post("/api/chat")
async def chat(q: ChatRequest, s: Session=Depends(get_session), r: Runtime=Depends(rt)):
    # The local planner is the tested source-of-truth answer path. When keys are
    # configured, Groq/Gemini performs bounded tool selection first; its output
    # cannot bypass the guarded dispatcher or deterministic evaluator.
    routed=await hosted_route(q.message,s,r)
    result=local_answer(q.message,s,r)
    if routed:
        result["events"]=[*routed,*result["events"]]
        result["model_routing"]="groq_or_gemini"
    else:
        result["model_routing"]="local_fallback"
    result["turn_id"]=q.turn_id or str(uuid.uuid4()); return result
@app.post("/api/actions/{proposal_id}/confirm")
def confirm(proposal_id: str, q: ConfirmRequest, s: Session=Depends(get_session), r: Runtime=Depends(rt)): return r.actions.confirm(proposal_id,q.confirmed,s)
