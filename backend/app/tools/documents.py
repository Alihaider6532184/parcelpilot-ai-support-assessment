from __future__ import annotations
import math, re
from collections import Counter
from ..reliability.rules import rank_sources
from ..data.embeddings import embed_text

STOPWORDS={"a","an","and","are","any","about","can","current","does","explain","for","from","guidance","how","i","in","is","it","me","of","on","or","say","the","their","this","to","what","when","which","with"}
SYNONYMS={
    "cancel":{"cancellation","cancelled","booked","fee"},
    "cancellation":{"cancel","cancelled","booked","fee"},
    "response":{"first","target","targets","sla"},
    "times":{"time","target","targets","response"},
    "critical":{"p1","severity","outage"},
    "credit":{"credits","service","pickup","delay"},
    "credits":{"credit","service","pickup","delay"},
    "late":{"delay","pickup","credit"},
    "bulk":{"upload","csv","rows"},
    "upload":{"bulk","csv","rows"},
    "webhook":{"swiftship","pickup","delay"},
    "limit":{"limits","rows","supported"},
    "limits":{"limit","rows","supported"},
}


def _tokens(value: str) -> set[str]:
    raw={x for x in re.findall(r"[a-z0-9]+",value.lower()) if x not in STOPWORDS}
    expanded=set(raw)
    for token in raw: expanded.update(SYNONYMS.get(token,set()))
    return expanded


def _topics(query: str) -> set[str]:
    words=_tokens(query); found=set()
    if words & {"cancel","cancellation","cancelled","fee","booked"}: found.add("cancellation")
    if words & {"credit","credits","late","delay"}: found.add("service_credit")
    if words & {"p1","p2","p3","critical","severity"}: found.add("severity")
    if words & {"response","targets","sla"}: found.add("sla")
    if words & {"bulk","upload","csv"}: found.update({"bulk_upload","product_issue"})
    if words & {"webhook","swiftship","ki","issue"}: found.update({"pickup","product_issue"})
    return found


class DocumentTool:
    def __init__(self, docs, chroma_dir): self.docs,self.chroma_dir=docs,chroma_dir

    def _semantic_scores(self, query: str) -> tuple[dict[str,float],str]:
        try:
            import chromadb
            collection=chromadb.PersistentClient(path=str(self.chroma_dir)).get_collection("parcelpilot_documents")
            count=collection.count()
            if not count: return {},"lexical"
            # Supplying the embedding explicitly prevents Chroma from loading
            # its default ONNX model again inside a request worker.
            result=collection.query(query_embeddings=[embed_text(query)],n_results=min(24,count),include=["distances"])
            distances=(result.get("distances") or [[]])[0]; ids=(result.get("ids") or [[]])[0]
            return {doc_id:max(0.0,1.0-float(distance)) for doc_id,distance in zip(ids,distances)},"hybrid_chroma"
        except Exception:
            return {},"lexical"

    def _query_account(self, q, session) -> str | None:
        requested=(q.account_id or "").upper() or None; query=q.query.lower(); mentioned=[]
        for doc in self.docs:
            meta=doc["metadata"]; account_id=meta.get("account_id") or None; name=meta.get("account_name") or ""
            if account_id and name:
                name_tokens=[x for x in re.findall(r"[a-z0-9]+",name.lower()) if len(x)>=4]
                if name.lower() in query or any(token in _tokens(query) for token in name_tokens): mentioned.append(account_id)
        account_id=requested or next(iter(dict.fromkeys(mentioned)),None)
        if account_id and not session.all_accounts and account_id not in session.allowed_account_ids: return None
        return account_id

    def search(self, q, session):
        query_tokens=_tokens(q.query); requested_topics=set(q.topics) | _topics(q.query)
        account_id=self._query_account(q,session); semantic,retrieval_mode=self._semantic_scores(q.query)
        # Score the individual chunk's content, not document-level metadata.
        # Otherwise every section of a high-authority agreement looks equally
        # relevant merely because the whole document is tagged "cancellation".
        tokenized={d["id"]:_tokens(" ".join([d["text"],d["metadata"].get("section","")])) for d in self.docs}
        frequency=Counter(token for tokens in tokenized.values() for token in query_tokens & tokens)
        exact_ids=set(re.findall(r"\b(?:KI|ORD|TKT|ACCT)-\d+\b",q.query.upper())); candidates=[]
        for d in self.docs:
            meta=d["metadata"]; doc_account=meta.get("account_id") or None
            if meta.get("status") in {"deprecated","context_only"} and not q.include_context_only: continue
            if doc_account:
                if account_id != doc_account: continue
                if not session.all_accounts and doc_account not in session.allowed_account_ids: continue
            matched=query_tokens & tokenized[d["id"]]
            lexical=sum(1.0+math.log((len(self.docs)+1)/(frequency[token]+1)) for token in matched)
            section_hits=len(query_tokens & _tokens(meta.get("section","")))
            topic_hits=len(requested_topics & set(meta.get("topics","").split(",")))
            exact_bonus=12.0 if exact_ids and any(value in d["text"].upper() or value in meta.get("section","").upper() for value in exact_ids) else 0.0
            account_bonus=3.0 if account_id and doc_account==account_id else 0.0
            capability_bonus=10.0 if query_tokens & {"plan","limit","limits","available","supported"} and "plan capabilities" in meta.get("section","").lower() else 0.0
            semantic_bonus=semantic.get(d["id"],0.0)*2.5
            authority_tiebreak=float(meta.get("authority_rank",0))/1000
            score=lexical+(section_hits*0.8)+(topic_hits*0.9)+exact_bonus+account_bonus+capability_bonus+semantic_bonus+authority_tiebreak
            if matched or exact_bonus or semantic.get(d["id"],0.0)>.25:
                candidates.append({"citation_id":d["id"],"text":d["text"],**meta,"score":round(score,4)})
        return {"results":rank_sources(candidates,account_id)[:8],"retrieval_warnings":[],"retrieval_mode":retrieval_mode,"resolved_account_id":account_id}
