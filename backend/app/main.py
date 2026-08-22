from __future__ import annotations
import json, re, uuid
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
import jwt
from .config import SESSION_SECRET, FRONTEND_ORIGIN, GROQ_API_KEY, GEMINI_API_KEY, GROQ_MODEL, GEMINI_MODEL
from .schemas.models import *
from .services.runtime import Runtime
from .agent.providers import Provider, SYSTEM, TOOLS
from .agent.local import infer
from .reliability.rules import evaluate

app=FastAPI(title="ParcelPilot AI Support", version="1.0.0")
app.add_middleware(CORSMiddleware,allow_origins=[FRONTEND_ORIGIN,"http://localhost:3000"],allow_credentials=True,allow_methods=["*"],allow_headers=["*"])
runtime: Runtime|None=None; provider=Provider(GROQ_API_KEY,GEMINI_API_KEY,GROQ_MODEL,GEMINI_MODEL)
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
        return evaluate(lookup["record"]["fields"],account,[{"citation_id":d["id"],**d["metadata"]} for d in r.docs],q.evaluation_type,q.reported_pickup_at,r.dataset_now)
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
            answer=f"For {oid}, the deterministic evaluation is **{ev['result']}**. Cancellation fee: INR {ev['fee_inr']}. {ev['recommended_next_step']}. Governing citations: {', '.join(ev['governing_sources'])}."
        elif oid and any(k in low for k in ("credit","late","pickup","carrier")):
            events.append({"type":"tool","name":"search_documents","status":"running"}); d=r.documents.search(DocumentQuery(query="failed pickup service credit carrier fault threshold",account_id=rec.get("account_id")),s); events[-1]["status"]="complete"; events[-1]["result"]=d
            answer=f"I found {oid} for account {rec.get('account_id')}. A pickup-time observation is required before promising a credit. The current evidence is scoped to the account and the SOP/agreement citations are shown in the tool results."
        else: answer=f"Authorized record lookup for {rec.get('account_id')}: {json.dumps(rec,default=str)}"
    else:
        answer="I found the following authoritative passages. I excluded deprecated policy and unverified ticket history from the current answer: " + " ".join(x["text"] for x in first.get("results",[])[:3])
    return {"answer":answer,"events":events,"dataset_now":r.dataset_now}

@app.post("/api/chat")
def chat(q: ChatRequest, s: Session=Depends(get_session), r: Runtime=Depends(rt)):
    # The local planner is deterministic and fully runnable without API keys.
    # With keys, provider integration can be enabled without weakening guards.
    result=local_answer(q.message,s,r); result["turn_id"]=q.turn_id or str(uuid.uuid4()); return result
@app.post("/api/actions/{proposal_id}/confirm")
def confirm(proposal_id: str, q: ConfirmRequest, s: Session=Depends(get_session), r: Runtime=Depends(rt)): return r.actions.confirm(proposal_id,q.confirmed,s)
