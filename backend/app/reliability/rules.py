from __future__ import annotations
from datetime import datetime
import re
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
    # Relevance remains the primary order. Authority resolves close topical
    # matches; it must never promote an unrelated policy passage above a direct
    # SOP, contract, or known-issue match.
    return sorted([r for r in results if r["applicable"]], key=lambda x: (-float(x.get("score",0)), -int(x.get("authority_rank",0)), x.get("file_name","")))


def _governing_sources(docs: list[dict[str,Any]], account_id: str, evaluation_type: str) -> list[dict[str,Any]]:
    needles={"cancellation":{"cancellation","cancel","booked"},"service_credit":{"credit","failed-pickup","pickup"}}[evaluation_type]
    agreement=[]; sop=[]
    for doc in docs:
        text=(str(doc.get("section",""))+" "+str(doc.get("text",""))).lower()
        if not any(needle in text for needle in needles): continue
        if doc.get("source_type")=="agreement" and doc.get("account_id")==account_id and doc.get("status")=="active": agreement.append(doc)
        elif doc.get("source_type")=="sop" and doc.get("status")=="current": sop.append(doc)
    agreement.sort(key=lambda d:(-sum(needle in str(d.get("section","")).lower() for needle in needles),d.get("section")=="Document overview"))
    sop.sort(key=lambda d:(-sum(needle in str(d.get("section","")).lower() for needle in needles),d.get("section")=="Document overview"))
    return [*agreement[:1],*sop[:1]]

def evaluate(order: dict[str, Any], account: dict[str, Any], docs: list[dict[str, Any]], evaluation_type: str, reported_pickup_at: str | None, dataset_now: str):
    order_id, account_id = order["order_id"], account["account_id"]
    sources=_governing_sources(docs,account_id,evaluation_type)
    agreement=[d for d in sources if d.get("source_type")=="agreement"]
    citations = [d["citation_id"] for d in sources]
    clauses=[{"citation_id":d["citation_id"],"file_name":d.get("file_name"),"section":d.get("section"),"source_type":d.get("source_type"),"authority_rank":d.get("authority_rank")} for d in sources]
    facts = {"account_name":account.get("account_name"),"carrier":order.get("carrier"),"status":order.get("status"),"booked_at":order.get("booked_at"),"pickup_window_end":order.get("pickup_window_end"),"pickup_actual_at":order.get("pickup_actual_at"),"observed_or_reported_at":reported_pickup_at,"shipment_fee_inr":order.get("shipment_fee_inr"),"carrier_fault":bool(order.get("carrier_fault")),"customer_fault":bool(order.get("customer_fault"))}
    missing=[]
    if evaluation_type == "cancellation":
        if order.get("status") == "DRAFT":
            return {"order_id":order_id,"account_id":account_id,"account_name":account.get("account_name"),"evaluation_type":evaluation_type,"result":"eligible","fee_inr":0,"credit_inr":0,"governing_sources":citations,"governing_clauses":clauses,"facts_used":facts,"missing_or_conflicting_facts":[],"manager_approval_required":False,"recommended_next_step":"Cancel with no fee; the order is still DRAFT"}
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
            waived=fee==0 and cancellation_waived
            return {"order_id":order_id,"account_id":account_id,"account_name":account.get("account_name"),"evaluation_type":evaluation_type,"result":"needs_verification" if missing else "eligible","fee_inr":fee,"credit_inr":0,"full_waiver":waived,"governing_sources":citations,"governing_clauses":clauses,"facts_used":facts,"missing_or_conflicting_facts":missing,"manager_approval_required":False,"recommended_next_step":"Cancel with no fee under the customer agreement's full waiver" if waived and not missing else "Cancel with no fee" if fee==0 and not missing else "Cancel with the applicable INR 250 fee" if fee==250 and not missing else "Verify timestamps before cancellation"}
        return {"order_id":order_id,"account_id":account_id,"account_name":account.get("account_name"),"evaluation_type":evaluation_type,"result":"not_eligible","fee_inr":0,"credit_inr":0,"full_waiver":False,"governing_sources":citations,"governing_clauses":clauses,"facts_used":facts,"missing_or_conflicting_facts":[],"manager_approval_required":False,"recommended_next_step":"Use return-to-origin workflow"}
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
            facts["delay_hours"]=round(hours,2)
            agreement_text=" ".join(str(d.get("text","")) for d in agreement).lower()
            fixed_match=re.search(r"more than\s+(\d+(?:\.\d+)?)\s+hours.*?fixed\s+inr\s+([\d,]+)",agreement_text)
            threshold=float(fixed_match.group(1)) if fixed_match else 2.0
            fixed_credit=int(fixed_match.group(2).replace(",","")) if fixed_match else None
            if hours > threshold and order["carrier_fault"] and not order["customer_fault"]:
                credit=fixed_credit if fixed_credit is not None else min(500,round(float(order["shipment_fee_inr"])*.1))
            else: credit=0
            result="eligible" if credit else "not_eligible"
        except Exception: missing.append("valid pickup-window or pickup timestamp"); result="needs_verification"; credit=0
    return {"order_id":order_id,"account_id":account_id,"account_name":account.get("account_name"),"evaluation_type":evaluation_type,"result":result,"fee_inr":0,"credit_inr":credit,"governing_sources":citations,"governing_clauses":clauses,"facts_used":facts,"missing_or_conflicting_facts":missing,"manager_approval_required":credit>1000,"recommended_next_step":"Offer the calculated credit" if credit else "No credit is owed under the verified facts" if result=="not_eligible" else "Verify facts or escalate"}
