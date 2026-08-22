from __future__ import annotations
import sqlite3
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
