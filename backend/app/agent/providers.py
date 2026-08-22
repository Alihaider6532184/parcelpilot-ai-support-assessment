from __future__ import annotations
import asyncio, json, uuid
import httpx

TOOLS = [
 {"type":"function","function":{"name":"search_documents","description":"Use only for general policy, contract, SOP, product-guide, or how-to guidance. Do not use for account/order/ticket facts, cross-account record requests, aggregate issue analysis, calculations, or action requests.","parameters":{"type":"object","properties":{"query":{"type":"string"},"account_id":{"type":["string","null"]},"topics":{"type":"array","items":{"type":"string"}}},"required":["query"]}}},
 {"type":"function","function":{"name":"lookup_records","description":"Fetch factual account, order, ticket, complaint, status, history, or customer data. Use even when no ID is supplied. For requests about another agent/customer/account choose query_scope=other_accounts so the repository can explicitly authorize or deny it.","parameters":{"type":"object","properties":{"record_type":{"type":"string","enum":["account","order","ticket"]},"record_id":{"type":["string","null"]},"include_related":{"type":"boolean"},"query_scope":{"type":"string","enum":["assigned_accounts","other_accounts","all_accounts"]}},"required":["record_type","query_scope"]}}},
 {"type":"function","function":{"name":"evaluate_entitlement","description":"Use for a specific order's cancellation fee, cancellation eligibility, SLA credit, failed-pickup credit, or other deterministic entitlement calculation.","parameters":{"type":"object","properties":{"order_id":{"type":"string"},"evaluation_type":{"type":"string","enum":["cancellation","service_credit"]},"reported_pickup_at":{"type":["string","null"]}},"required":["order_id","evaluation_type"]}}},
 {"type":"function","function":{"name":"analyze_operations","description":"Analyze real ticket data for recurring complaints, product-issue patterns, or trends across multiple customers/accounts. Use for aggregate or cross-customer questions, not document policy search.","parameters":{"type":"object","properties":{"analysis_type":{"type":"string","enum":["recurring_ticket_issues"]},"scope":{"type":"string","enum":["assigned_accounts","all_accounts"]},"min_accounts":{"type":"integer","minimum":2,"maximum":10},"include_closed":{"type":"boolean"}},"required":["analysis_type","scope"]}}},
 {"type":"function","function":{"name":"propose_escalation","description":"Use for any request to create, open, raise, or start an escalation/follow-up. The server resolves and authorizes IDs and returns a pending-confirmation draft; viewers are explicitly denied. Never substitute document search when this action is requested.","parameters":{"type":"object","properties":{"account_id":{"type":["string","null"]},"order_id":{"type":["string","null"]},"ticket_id":{"type":["string","null"]},"reason":{"type":"string"},"severity":{"type":"string","enum":["P1","P2","P3"]},"evidence_citation_ids":{"type":"array","items":{"type":"string"}}},"required":["reason","severity"]}}},
]
SYSTEM = """You are the tool router for an internal ParcelPilot support assistant. You must choose exactly one provided function for every user turn. Select by intent, not by the presence of an ID. Record/customer/order/ticket facts use lookup_records; requests for another customer's data still use lookup_records with other_accounts so authorization can deny them; recurring issues across customers use analyze_operations; cancellation or credit calculations use evaluate_entitlement; create/open/escalate requests use propose_escalation; only general policy/how-to guidance uses search_documents. Never replace a denied or action intent with document search. Never invent an ID. The server—not you—enforces role and account access."""

def _gemini_schema(value):
    if isinstance(value, dict):
        out={k:_gemini_schema(v) for k,v in value.items()}
        if isinstance(out.get("type"), list): out["type"] = next((x for x in out["type"] if x != "null"), "string")
        return out
    if isinstance(value, list): return [_gemini_schema(v) for v in value]
    return value

async def _retry(client, method, url, **kwargs):
    last=None
    for i in range(2):
        try:
            r=await client.request(method,url,**kwargs)
            if r.status_code < 400: return r
            if r.status_code not in (408,409,425,429,500,502,503,504): r.raise_for_status()
            last=r
        except Exception as e: last=e
        await asyncio.sleep(0.25*(2**i))
    if isinstance(last,Exception): raise last
    last.raise_for_status()

class Provider:
    def __init__(self, groq_key="", gemini_key="", groq_model="openai/gpt-oss-20b", gemini_model="gemini-3.6-flash"):
        self.groq_key,self.gemini_key,self.groq_model,self.gemini_model=groq_key,gemini_key,groq_model,gemini_model
    async def complete(self, messages, tools, tool_choice="required"):
        """Return a normalized assistant message or None on quota/network failure.

        Groq is attempted first. Gemini uses its native function-declaration
        format as a fallback. The caller treats the result as routing guidance;
        all data access and decisions still pass through guarded server tools.
        """
        async with httpx.AsyncClient(timeout=18) as client:
            if self.groq_key:
                try:
                    r=await _retry(client,"POST","https://api.groq.com/openai/v1/chat/completions",headers={"Authorization":f"Bearer {self.groq_key}"},json={"model":self.groq_model,"messages":messages,"tools":tools,"tool_choice":tool_choice,"parallel_tool_calls":False,"temperature":0,"max_completion_tokens":512}); out=r.json()["choices"][0]["message"]; out["_provider"]="groq"; return out
                except Exception: pass
            if self.gemini_key:
                contents=[]
                system_text=[]
                for m in messages:
                    text=m.get("content") or ""
                    if m.get("role") == "system": system_text.append(text); continue
                    role="user" if m.get("role") == "user" else "model"
                    if text: contents.append({"role":role,"parts":[{"text":text}]})
                declarations=[_gemini_schema(t["function"]) for t in tools]
                r=await _retry(client,"POST",f"https://generativelanguage.googleapis.com/v1beta/models/{self.gemini_model}:generateContent?key={self.gemini_key}",json={"systemInstruction":{"parts":[{"text":"\n".join(system_text) or SYSTEM}]},"contents":contents,"tools":[{"function_declarations":declarations}],"toolConfig":{"functionCallingConfig":{"mode":"ANY"}},"generationConfig":{"temperature":0,"maxOutputTokens":512}})
                parts=r.json()["candidates"][0]["content"]["parts"]; calls=[]; text_parts=[]
                for p in parts:
                    if "functionCall" in p:
                        fc=p["functionCall"]; calls.append({"id":str(uuid.uuid4()),"type":"function","function":{"name":fc["name"],"arguments":json.dumps(fc.get("args",{}))}})
                    if "text" in p: text_parts.append(p["text"])
                return {"role":"assistant","content":" ".join(text_parts),"tool_calls":calls,"_provider":"gemini"}
        return None
