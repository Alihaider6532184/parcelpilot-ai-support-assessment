from __future__ import annotations
import sqlite3
import re
from datetime import datetime
from typing import Any
from fastapi import HTTPException
from ..schemas.models import Session

class Repository:
    def __init__(self, db_path, dataset_now: str): self.db_path, self.dataset_now = db_path, dataset_now
    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c
    def _allowed(self, session: Session, account_id: str):
        if not session.all_accounts and account_id not in session.allowed_account_ids:
            raise HTTPException(status_code=403, detail="Account is outside this session scope")
    def _account_for(self, conn, record_type, record_id):
        table = {"account": "accounts", "order": "orders", "ticket": "tickets"}[record_type]
        key = {"account": "account_id", "order": "order_id", "ticket": "ticket_id"}[record_type]
        row = conn.execute(f"SELECT * FROM [{table}] WHERE {key} = ?", (record_id,)).fetchone()
        if row is None: raise HTTPException(status_code=404, detail="Record not found")
        return dict(row)
    def lookup(self, q, session: Session):
        with self._conn() as conn:
            if not q.record_id:
                if q.query_scope == "other_accounts" and not session.all_accounts:
                    raise HTTPException(status_code=403, detail=f"Cross-account record access is not permitted for role {session.role}")
                if q.query_scope == "all_accounts" and not session.all_accounts:
                    raise HTTPException(status_code=403, detail=f"All-account record access is not permitted for role {session.role}")
                table={"account":"accounts","order":"orders","ticket":"tickets"}[q.record_type]
                if session.all_accounts or q.query_scope in {"all_accounts","other_accounts"}:
                    rows=[dict(x) for x in conn.execute(f"SELECT * FROM [{table}] ORDER BY 1")]
                elif not session.allowed_account_ids:
                    rows=[]
                else:
                    marks=",".join("?" for _ in session.allowed_account_ids)
                    rows=[dict(x) for x in conn.execute(f"SELECT * FROM [{table}] WHERE account_id IN ({marks}) ORDER BY 1",tuple(session.allowed_account_ids))]
                account_ids=sorted({x["account_id"] for x in rows})
                related_by_account={}
                if q.include_related:
                    for account_id in account_ids:
                        related_by_account[account_id]={
                            "account":dict(conn.execute("SELECT * FROM accounts WHERE account_id=?",(account_id,)).fetchone()),
                            "orders":[dict(x) for x in conn.execute("SELECT * FROM orders WHERE account_id=?",(account_id,))],
                            "tickets":[dict(x) for x in conn.execute("SELECT * FROM tickets WHERE account_id=?",(account_id,))],
                        }
                return {"dataset_now":self.dataset_now,"record":{"record_type":q.record_type,"fields":{}},"records":rows,"related":{},"related_by_account":related_by_account,"scope":{"account_ids":account_ids,"authorized":True,"query_scope":q.query_scope}}
            row = self._account_for(conn, q.record_type, q.record_id)
            account_id = row["account_id"] if q.record_type != "account" else row["account_id"]
            self._allowed(session, account_id)
            related = {"account": {}, "orders": [], "tickets": []}
            if q.include_related:
                related["account"] = dict(conn.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone())
                related["orders"] = [dict(x) for x in conn.execute("SELECT * FROM orders WHERE account_id=?", (account_id,))]
                related["tickets"] = [dict(x) for x in conn.execute("SELECT * FROM tickets WHERE account_id=?", (account_id,))]
            return {"dataset_now": self.dataset_now, "record": {"record_type": q.record_type, "fields": row}, "related": related, "scope": {"account_id": account_id, "authorized": True}}
    def order(self, order_id: str, session: Session):
        class Q: pass
        q=Q(); q.record_type="order"; q.record_id=order_id; q.include_related=True
        return self.lookup(q, session)
    def account(self, account_id: str, session: Session):
        class Q: pass
        q=Q(); q.record_type="account"; q.record_id=account_id; q.include_related=True
        return self.lookup(q, session)

    def _account_from_scenario(self, conn, text: str, session: Session):
        words=set(re.findall(r"[a-z0-9]+",(text or "").lower()))
        matches=[]
        for raw in conn.execute("SELECT * FROM accounts ORDER BY account_id"):
            row=dict(raw); name_words={x for x in re.findall(r"[a-z0-9]+",row["account_name"].lower()) if len(x)>=4}
            score=len(words & name_words)
            if score: matches.append((score,row))
        if matches:
            matches.sort(key=lambda item:(-item[0],item[1]["account_id"])); account=matches[0][1]
            self._allowed(session,account["account_id"]); return account
        if not session.all_accounts and len(session.allowed_account_ids)==1:
            row=conn.execute("SELECT * FROM accounts WHERE account_id=?",(session.allowed_account_ids[0],)).fetchone()
            return dict(row) if row else None
        return None

    def resolve_entitlement_order(self, q, session: Session):
        """Resolve an authorized concrete order from an ID or supplied scenario."""
        if q.order_id: return self.order(q.order_id,session)
        with self._conn() as conn:
            account=None
            if q.account_id:
                account=self._account_for(conn,"account",q.account_id); self._allowed(session,account["account_id"])
            else:
                account=self._account_from_scenario(conn," ".join(x for x in (q.customer_name,q.scenario_text) if x),session)
            if not account:
                raise HTTPException(status_code=422,detail="A customer or order must be identifiable for this calculation")
            orders=[dict(row) for row in conn.execute("SELECT * FROM orders WHERE account_id=? ORDER BY order_id",(account["account_id"],))]
            candidates=[]
            for order in orders:
                if q.evaluation_type=="cancellation":
                    if order.get("status") not in {"DRAFT","BOOKED"} or order.get("pickup_actual_at"): continue
                    score=5
                    if q.booking_age_hours is not None:
                        try:
                            now=datetime.fromisoformat(self.dataset_now[:16]); booked=datetime.fromisoformat(str(order["booked_at"]).replace(" ","T")); score-=abs(((now-booked).total_seconds()/3600)-q.booking_age_hours)
                        except Exception: pass
                    candidates.append((score,order))
                else:
                    if q.carrier_fault is True and not bool(order.get("carrier_fault")): continue
                    if q.customer_fault is False and bool(order.get("customer_fault")): continue
                    score=0
                    if bool(order.get("carrier_fault")): score+=4
                    if not bool(order.get("customer_fault")): score+=2
                    if not order.get("pickup_actual_at"): score+=1
                    try:
                        now=datetime.fromisoformat(self.dataset_now[:16]); end=datetime.fromisoformat(str(order["pickup_window_end"]).replace(" ","T"))
                        if now > end: score+=3
                        if q.delay_hours is not None: score-=abs(((now-end).total_seconds()/3600)-q.delay_hours)*.1
                    except Exception: pass
                    candidates.append((score,order))
            if not candidates:
                raise HTTPException(status_code=422,detail=f"No authorized order matches the supplied {q.evaluation_type.replace('_',' ')} facts")
            candidates.sort(key=lambda item:(-item[0],item[1]["order_id"])); best_score,best=candidates[0]
            if len(candidates)>1 and abs(best_score-candidates[1][0])<.01:
                raise HTTPException(status_code=422,detail="More than one authorized order matches; provide an order ID")
            order_id=best["order_id"]
        return self.order(order_id,session)
