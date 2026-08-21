from __future__ import annotations
import json, sqlite3, uuid
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from ..schemas.models import Session

class ActionTool:
    def __init__(self, path):
        self.path=path; path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS proposals (proposal_id TEXT PRIMARY KEY, user_id TEXT, account_id TEXT, payload TEXT, status TEXT, expires_at TEXT, action_id TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS actions (action_id TEXT PRIMARY KEY, proposal_id TEXT UNIQUE, user_id TEXT, account_id TEXT, payload TEXT, created_at TEXT)")
    def propose(self, q, session):
        if session.role == "viewer": raise HTTPException(status_code=403, detail="Viewer cannot propose actions")
        if not session.all_accounts and q.account_id not in session.allowed_account_ids: raise HTTPException(status_code=403, detail="Account is outside this session scope")
        pid=str(uuid.uuid4()); expires=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat(); payload=q.model_dump()
        with sqlite3.connect(self.path) as c: c.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?,?)",(pid,session.user_id,q.account_id,json.dumps(payload),"pending_confirmation",expires,None))
        return {"proposal_id":pid,"status":"pending_confirmation","summary":q.reason,"payload_preview":payload,"expires_at":expires,"confirmation_phrase":"Confirm escalation"}
    def confirm(self, proposal_id, confirmed, session):
        if not confirmed: raise HTTPException(status_code=400, detail="Explicit confirmation is required")
        with sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row; row=c.execute("SELECT * FROM proposals WHERE proposal_id=?",(proposal_id,)).fetchone()
            if not row: raise HTTPException(status_code=404, detail="Proposal not found")
            if row["user_id"] != session.user_id: raise HTTPException(status_code=403, detail="Proposal belongs to another session")
            if not session.all_accounts and row["account_id"] not in session.allowed_account_ids: raise HTTPException(status_code=403, detail="Account is outside this session scope")
            if row["status"] == "confirmed": return {"action_id":row["action_id"],"status":"created","created_at":None,"action":json.loads(row["payload"])}
            if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc): raise HTTPException(status_code=409, detail="Proposal expired")
            aid=str(uuid.uuid4()); now=datetime.now(timezone.utc).isoformat()
            c.execute("INSERT INTO actions VALUES (?,?,?,?,?,?)",(aid,proposal_id,session.user_id,row["account_id"],row["payload"],now)); c.execute("UPDATE proposals SET status='confirmed', action_id=? WHERE proposal_id=?",(aid,proposal_id))
            return {"action_id":aid,"status":"created","created_at":now,"action":json.loads(row["payload"])}
