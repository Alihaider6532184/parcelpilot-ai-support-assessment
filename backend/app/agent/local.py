import re
from ..schemas.models import DocumentQuery, LookupQuery, EvaluateQuery, ProposalQuery

def infer(message: str):
    ids=re.findall(r"(?:ORD|TKT|ACCT)-\d+",message.upper()); oid=next((x for x in ids if x.startswith("ORD-")),None); tid=next((x for x in ids if x.startswith("TKT-")),None); aid=next((x for x in ids if x.startswith("ACCT-")),None)
    low=message.lower()
    if oid: return [("lookup_records", LookupQuery(record_type="order",record_id=oid,include_related=True))]
    if tid: return [("lookup_records", LookupQuery(record_type="ticket",record_id=tid,include_related=True))]
    if aid: return [("lookup_records", LookupQuery(record_type="account",record_id=aid,include_related=True))]
    return [("search_documents",DocumentQuery(query=message,account_id=aid,topics=[]))]
