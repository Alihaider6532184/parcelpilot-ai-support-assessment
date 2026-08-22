from __future__ import annotations
import re
from fastapi import HTTPException


STOPWORDS={"a","all","an","and","are","at","after","for","from","how","in","is","it","of","on","still","the","to","we","with"}


def _issue_tokens(subject: str) -> set[str]:
    words=[]
    for raw in re.findall(r"[a-z0-9]+",subject.lower()):
        if raw in STOPWORDS or raw.isdigit(): continue
        word=raw
        for suffix in ("ures","ure","ing","ed","es","s"):
            if word.endswith(suffix) and len(word)-len(suffix) >= 4:
                word=word[:-len(suffix)]; break
        words.append(word)
    return set(words)


class AnalyticsTool:
    def __init__(self, repository): self.repository=repository

    def analyze(self, q, session):
        if q.scope == "all_accounts" and not session.all_accounts:
            raise HTTPException(status_code=403,detail=f"Cross-account analytics is not permitted for role {session.role}")
        with self.repository._conn() as conn:
            params=[]; where=[]
            if not q.include_closed: where.append("status = 'open'")
            if not session.all_accounts:
                if not session.allowed_account_ids: return self._empty(q)
                marks=",".join("?" for _ in session.allowed_account_ids); where.append(f"account_id IN ({marks})"); params.extend(session.allowed_account_ids)
            clause=" WHERE "+" AND ".join(where) if where else ""
            tickets=[dict(x) for x in conn.execute("SELECT * FROM tickets"+clause+" ORDER BY created_at DESC",tuple(params))]
        groups=[]; used=set()
        for i,ticket in enumerate(tickets):
            if i in used: continue
            tokens=_issue_tokens(ticket.get("subject", "")); group=[ticket]
            for j,other in enumerate(tickets[i+1:],i+1):
                other_tokens=_issue_tokens(other.get("subject", "")); shared=tokens & other_tokens; union=tokens | other_tokens
                if len(shared) >= 2 and union and len(shared)/len(union) >= .35:
                    group.append(other); used.add(j)
            if len(group) >= 2:
                accounts=sorted({x["account_id"] for x in group})
                groups.append({"label":group[0]["subject"],"account_ids":accounts,"account_count":len(accounts),"ticket_ids":[x["ticket_id"] for x in group],"subjects":[x["subject"] for x in group]})
        recurring=[g for g in groups if g["account_count"] >= q.min_accounts]
        accounts=sorted({x["account_id"] for x in tickets})
        return {"dataset_now":self.repository.dataset_now,"analysis_type":q.analysis_type,"ticket_count":len(tickets),"accounts_analyzed":accounts,"minimum_accounts":q.min_accounts,"recurring_issues":recurring,"same_customer_repeats":[g for g in groups if g["account_count"] < q.min_accounts],"no_significant_recurring_issues":not recurring,"source":"SQLite tickets table"}

    def _empty(self,q):
        return {"dataset_now":self.repository.dataset_now,"analysis_type":q.analysis_type,"ticket_count":0,"accounts_analyzed":[],"minimum_accounts":q.min_accounts,"recurring_issues":[],"same_customer_repeats":[],"no_significant_recurring_issues":True,"source":"SQLite tickets table"}
