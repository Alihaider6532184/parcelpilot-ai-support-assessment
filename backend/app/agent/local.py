import re
from ..schemas.models import DocumentQuery, LookupQuery

def is_action_intent(message: str) -> bool:
    """Recognize a request to create work, rather than merely retrieve facts."""
    low = message.lower()
    patterns = (
        r"\b(?:please\s+)?escalate\b",
        r"\b(?:create|open|start|raise)\s+(?:an?\s+)?escalation\b",
        r"\b(?:create|open|start|raise)\s+(?:an?\s+)?(?:follow[- ]up\s+)?ticket\b",
        r"\b(?:create|open|start|raise)\s+(?:an?\s+)?follow[- ]up\b",
    )
    return any(re.search(pattern, low) for pattern in patterns)


def infer(message: str, context: dict | None = None):
    ids=re.findall(r"(?:ORD|TKT|ACCT)-\d+",message.upper()); oid=next((x for x in ids if x.startswith("ORD-")),None); tid=next((x for x in ids if x.startswith("TKT-")),None); aid=next((x for x in ids if x.startswith("ACCT-")),None)
    if oid: return [("lookup_records", LookupQuery(record_type="order",record_id=oid,include_related=True))]
    if tid: return [("lookup_records", LookupQuery(record_type="ticket",record_id=tid,include_related=True))]
    if aid: return [("lookup_records", LookupQuery(record_type="account",record_id=aid,include_related=True))]
    # A follow-up such as "please escalate this" deliberately re-loads the
    # last scoped record. It never trusts client state or bypasses repository
    # authorization, and the context is cleared on logout.
    if is_action_intent(message) and context:
        if context.get("order_id"):
            return [("lookup_records", LookupQuery(record_type="order",record_id=context["order_id"],include_related=True))]
        if context.get("ticket_id"):
            return [("lookup_records", LookupQuery(record_type="ticket",record_id=context["ticket_id"],include_related=True))]
        if context.get("account_id"):
            return [("lookup_records", LookupQuery(record_type="account",record_id=context["account_id"],include_related=True))]
    return [("search_documents",DocumentQuery(query=message,account_id=aid,topics=[]))]
