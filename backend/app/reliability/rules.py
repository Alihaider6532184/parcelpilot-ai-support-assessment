from __future__ import annotations
from datetime import datetime
from typing import Any

def source_allowed(meta: dict[str, Any], session_account: str | None, include_context=False) -> bool:
    status = meta.get("status", "")
    if status == "deprecated": return include_context
    if status == "context_only": return include_context
    account = meta.get("account_id") or None
    return account is None or account == session_account

def rank_sources(results: list[dict[str, Any]], account_id: str | None) -> list[dict[str, Any]]:
    for r in results:
        r["applicable"] = source_allowed(r, account_id)
        r["warnings"] = []
        if r.get("status") == "deprecated": r["warnings"].append("deprecated - excluded from current answer")
        if r.get("status") == "context_only": r["warnings"].append("historical context - unverified")
        if r.get("source_type") == "agreement" and r.get("account_id") == account_id: r["warnings"].append("contract override")
    return sorted([r for r in results if r["applicable"]], key=lambda x: (-int(x.get("authority_rank", 0)), x.get("file_name", "")))

def evaluate(order: dict[str, Any], account: dict[str, Any], docs: list[dict[str, Any]], evaluation_type: str, reported_pickup_at: str | None, dataset_now: str):
    order_id, account_id = order["order_id"], account["account_id"]
    agreement = [d for d in docs if d.get("source_type") == "agreement" and d.get("account_id") == account_id and d.get("status") == "active"]
    sop = [d for d in docs if d.get("source_type") == "sop" and d.get("status") == "current"]
    sources = [*(agreement[:2]), *(sop[:2])]
    citations = [d["citation_id"] for d in sources]
    facts = {"status": order.get("status"), "booked_at": order.get("booked_at"), "pickup_window_end": order.get("pickup_window_end"), "carrier_fault": order.get("carrier_fault"), "customer_fault": order.get("customer_fault")}
    missing=[]
    if evaluation_type == "cancellation":
        if order.get("status") == "BOOKED" and not order.get("pickup_actual_at"):
            fee = 0
            agreement_text = " ".join(str(d.get("text", "")) for d in agreement).lower()
            # An agreement is not automatically a cancellation-fee override:
            # LumenWorks explicitly says the default SOP applies, while
            # Northstar explicitly waives the fee before pickup.
            cancellation_waived = "no cancellation fee" in agreement_text and "no special cancellation-fee waiver applies" not in agreement_text
            if not cancellation_waived:
                try:
                    booked = datetime.fromisoformat(str(order["booked_at"]).replace(" ", "T")); req = datetime.fromisoformat(str(order["cancellation_requested_at"]).replace(" ", "T"))
                    fee = 0 if (req-booked).total_seconds() <= 1800 else 250
                except Exception: missing.append("booking or cancellation timestamp")
            return {"order_id": order_id,"account_id":account_id,"evaluation_type":evaluation_type,"result":"needs_verification" if missing else "eligible","fee_inr":fee,"credit_inr":0,"governing_sources":citations,"facts_used":facts,"missing_or_conflicting_facts":missing,"manager_approval_required":False,"recommended_next_step":"Cancel with no fee" if fee == 0 and not missing else "Cancel with the applicable INR 250 fee" if fee == 250 and not missing else "Verify timestamps before cancellation"}
        return {"order_id":order_id,"account_id":account_id,"evaluation_type":evaluation_type,"result":"not_eligible","fee_inr":0,"credit_inr":0,"governing_sources":citations,"facts_used":facts,"missing_or_conflicting_facts":[],"manager_approval_required":False,"recommended_next_step":"Use return-to-origin workflow"}
    # Service-credit facts must be explicit; reported_pickup_at is supplied by
    # the caller when the workbook does not have an actual pickup timestamp.
    if not reported_pickup_at: missing.append("observed pickup time")
    if order.get("carrier_fault") is None: missing.append("carrier fault")
    if order.get("customer_fault") is None: missing.append("customer fault")
    if missing: result="needs_verification"; credit=0
    else:
        try:
            end=datetime.fromisoformat(str(order["pickup_window_end"]).replace(" ","T")); actual=datetime.fromisoformat(reported_pickup_at.replace("Z","+00:00").replace(" ","T"))
            if actual.tzinfo is None: actual=actual.replace(tzinfo=end.tzinfo)
            if end.tzinfo is None: end=end.replace(tzinfo=actual.tzinfo)
            hours=(actual-end).total_seconds()/3600
            threshold=4 if account_id == "ACCT-002" else 2
            if account_id == "ACCT-002" and hours > 4 and order["carrier_fault"] and not order["customer_fault"]: credit=300
            elif hours > threshold and order["carrier_fault"] and not order["customer_fault"]: credit=min(500, round(float(order["shipment_fee_inr"])*.1))
            else: credit=0
            result="eligible" if credit else "not_eligible"
        except Exception: missing.append("valid pickup-window or pickup timestamp"); result="needs_verification"; credit=0
    return {"order_id":order_id,"account_id":account_id,"evaluation_type":evaluation_type,"result":result,"fee_inr":0,"credit_inr":credit,"governing_sources":citations,"facts_used":facts,"missing_or_conflicting_facts":missing,"manager_approval_required":credit>1000,"recommended_next_step":"Offer the calculated credit" if credit else "Verify facts or escalate"}
