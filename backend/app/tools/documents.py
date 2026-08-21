from __future__ import annotations
import json, re
from pathlib import Path
from ..reliability.rules import rank_sources

class DocumentTool:
    def __init__(self, docs, chroma_dir): self.docs, self.chroma_dir = docs, chroma_dir
    def search(self, q, session):
        terms=set(re.findall(r"[a-z0-9_]+", q.query.lower()))
        candidates=[]
        for d in self.docs:
            meta=d["metadata"]
            if meta.get("account_id") and meta.get("account_id") not in session.allowed_account_ids and not session.all_accounts: continue
            hay=(d["text"]+" "+meta.get("topics","")+" "+meta.get("file_name","")).lower()
            score=sum(1 for t in terms if t in hay)
            if score: candidates.append({"citation_id":d["id"],"text":d["text"],**meta,"score":score})
        return {"results":rank_sources(sorted(candidates,key=lambda x:-x["score"])[:8], q.account_id if q.account_id else None),"retrieval_warnings":[]}
