from __future__ import annotations
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSelection:
    name: str
    arguments: dict


def _tokens(message: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", message.lower()))


def _ids(message: str) -> tuple[str | None, str | None, str | None]:
    found=re.findall(r"(?:ORD|TKT|ACCT)-\d+",message.upper())
    return (
        next((x for x in found if x.startswith("ORD-")),None),
        next((x for x in found if x.startswith("TKT-")),None),
        next((x for x in found if x.startswith("ACCT-")),None),
    )


def _number_before(message: str, suffix: str) -> float | None:
    match=re.search(rf"(\d+(?:\.\d+)?)\s*{suffix}",message.lower())
    return float(match.group(1)) if match else None


def _entitlement_args(message: str, evaluation_type: str, order_id: str | None) -> dict:
    lower=message.lower()
    booking_age=_number_before(message,r"hours?\s+ago") if "book" in lower else None
    delay=_number_before(message,r"hours?\s+late")
    return {
        "order_id":order_id,
        "evaluation_type":evaluation_type,
        "reported_pickup_at":None,
        "scenario_text":message,
        "booking_age_hours":booking_age,
        "delay_hours":delay,
        "carrier_fault":True if re.search(r"carrier(?:\s+is|\s+was|\s+at)?\s+fault|carrier's fault",lower) else None,
        "customer_fault":False if re.search(r"no\s+customer(?:-caused)?\s+fault|customer\s+(?:is|was)\s+not\s+at\s+fault",lower) else None,
    }


def high_confidence_selection(message: str, context: dict | None = None) -> ToolSelection | None:
    """Safety invariants for intents where a wrong tool would hide a denial.

    Native model function-calling is the primary router. These narrow semantic
    guards prevent an action, explicit cross-account read, or aggregate request
    from ever being silently converted into document retrieval if a provider is
    unavailable or produces an unsafe selection.
    """
    words=_tokens(message); oid,tid,aid=_ids(message); context=context or {}
    # Explicit document entities and policy/capability questions are reads from
    # the governed PDF corpus. Keep these ahead of aggregate analytics so an
    # LLM cannot mistake "known issue" for a request to analyze live tickets.
    known_issue=re.search(r"\bKI-\d+\b",message,re.IGNORECASE)
    policy_read=(
        bool(words & {"policy","severity","definition","definitions","response","target","targets","sop"})
        and not bool(oid or tid or aid)
    )
    capability_read=(
        bool(words & {"plan","growth","enterprise","standard","capability","capabilities","limit","limits"})
        and bool(words & {"upload","uploads","bulk","rows","csv"})
        and not bool(oid or tid or aid)
    )
    if known_issue or policy_read or capability_read:
        return ToolSelection("search_documents",{"query":message,"account_id":context.get("account_id"),"topics":[]})
    action_nouns={"escalation","escalate","escalated","follow","followup"}
    action_verbs={"create","file","open","raise","start","escalate"}
    action=(bool(words & action_nouns) and bool(words & action_verbs)) or "escalate" in words or (bool(words & {"create","file","raise","start"}) and bool(words & {"ticket","tickets","case"})) or ({"open","ticket"} <= words and ("new" in words or "for" in words or bool(context)))
    if action:
        return ToolSelection("propose_escalation",{
            "account_id":aid or context.get("account_id"),
            "order_id":oid or context.get("order_id"),
            "ticket_id":tid or context.get("ticket_id"),
            "reason":message,
            "severity":"P1" if words & {"urgent","critical","security","outage"} else "P2",
            "evidence_citation_ids":[],
        })
    aggregate=bool(words & {"across","multiple","several","recurring","repeat","repeated","trend","trends","widespread"}) and bool(words & {"customer","customers","account","accounts","complaint","complaints","issue","issues","ticket","tickets"})
    if aggregate:
        cross_customer=bool(words & {"customers","accounts"}) or (bool(words & {"across","multiple","several"}) and bool(words & {"customer","account"}))
        return ToolSelection("analyze_operations",{"analysis_type":"recurring_ticket_issues","scope":"all_accounts" if cross_customer else "assigned_accounts","min_accounts":2,"include_closed":True})
    cross=bool(words & {"other","another","someone","else"}) and bool(words & {"customer","customers","account","accounts","client","clients","agent","portfolio"})
    if cross:
        record_type="order" if words & {"order","orders","shipment","shipments","history"} else "ticket" if words & {"ticket","tickets","complaint","complaints"} else "account"
        return ToolSelection("lookup_records",{"record_type":record_type,"record_id":None,"include_related":True,"query_scope":"other_accounts"})
    cancellation=bool(words & {"cancel","cancelling","cancellation","fee","charge","charges","cost"})
    cancellation_scenario=bool(oid) or (cancellation and bool(words & {"shipment","order","booked","picked","pickup"}) and bool(words & {"ago","booked","picked","pickup","fee"}))
    if cancellation and cancellation_scenario:
        return ToolSelection("evaluate_entitlement",_entitlement_args(message,"cancellation",oid))
    service_credit=bool(words & {"credit","credits","owed"}) and bool(words & {"late","pickup","carrier","fault","sla","owed"})
    if (oid and words & {"credit","owed","late","pickup","carrier","sla"}) or service_credit:
        return ToolSelection("evaluate_entitlement",_entitlement_args(message,"service_credit",oid))
    if oid: return ToolSelection("lookup_records",{"record_type":"order","record_id":oid,"include_related":True,"query_scope":"assigned_accounts"})
    if tid: return ToolSelection("lookup_records",{"record_type":"ticket","record_id":tid,"include_related":True,"query_scope":"assigned_accounts"})
    if aid: return ToolSelection("lookup_records",{"record_type":"account","record_id":aid,"include_related":True,"query_scope":"assigned_accounts"})
    return None


def infer_fallback(message: str, context: dict | None = None) -> ToolSelection:
    """Quota/network fallback; production routing normally comes from the LLM."""
    context=context or {}; guarded=high_confidence_selection(message,context)
    if guarded: return guarded
    words=_tokens(message); oid,tid,aid=_ids(message)
    if words & {"order","orders","shipment","shipments","history","account","accounts","customer","customers","ticket","tickets","complaint","complaints","status","details","records"}:
        record_type="ticket" if words & {"ticket","tickets","complaint","complaints"} else "order" if words & {"order","orders","shipment","shipments","history","status"} else "account"
        return ToolSelection("lookup_records",{"record_type":record_type,"record_id":None,"include_related":True,"query_scope":"assigned_accounts"})
    return ToolSelection("search_documents",{"query":message,"account_id":context.get("account_id"),"topics":[]})
