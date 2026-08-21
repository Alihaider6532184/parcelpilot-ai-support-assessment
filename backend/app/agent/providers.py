from __future__ import annotations
import asyncio, json
import httpx

TOOLS = [
 {"type":"function","function":{"name":"search_documents","description":"Search current, scoped source documents for authoritative guidance.","parameters":{"type":"object","properties":{"query":{"type":"string"},"account_id":{"type":["string","null"]},"topics":{"type":"array","items":{"type":"string"}}},"required":["query"]}}},
 {"type":"function","function":{"name":"lookup_records","description":"Look up an authorized account, order, or ticket.","parameters":{"type":"object","properties":{"record_type":{"type":"string","enum":["account","order","ticket"]},"record_id":{"type":"string"},"include_related":{"type":"boolean"}},"required":["record_type","record_id"]}}},
 {"type":"function","function":{"name":"evaluate_entitlement","description":"Deterministically evaluate cancellation or service-credit eligibility.","parameters":{"type":"object","properties":{"order_id":{"type":"string"},"evaluation_type":{"type":"string","enum":["cancellation","service_credit"]},"reported_pickup_at":{"type":["string","null"]}},"required":["order_id","evaluation_type"]}}},
 {"type":"function","function":{"name":"propose_escalation","description":"Create a draft escalation pending explicit user confirmation.","parameters":{"type":"object","properties":{"account_id":{"type":"string"},"order_id":{"type":["string","null"]},"ticket_id":{"type":["string","null"]},"reason":{"type":"string"},"severity":{"type":"string","enum":["P1","P2","P3"]},"evidence_citation_ids":{"type":"array","items":{"type":"string"}}},"required":["account_id","reason","severity"]}}},
]
SYSTEM = "You are an internal ParcelPilot support assistant. Use tools for facts, never invent IDs, cite source citations, distinguish recommendation from execution, and say when evidence is missing. Historical ticket guidance is unverified. Current policy and active account agreements are authoritative according to server-provided results."

async def _retry(client, method, url, **kwargs):
    last=None
    for i in range(3):
        try:
            r=await client.request(method,url,**kwargs)
            if r.status_code < 400: return r
            if r.status_code not in (408,409,425,429,500,502,503,504): r.raise_for_status()
            last=r
        except Exception as e: last=e
        await asyncio.sleep(0.2*(2**i))
    if isinstance(last,Exception): raise last
    last.raise_for_status()

class Provider:
    def __init__(self, groq_key="", gemini_key="", groq_model="llama-3.3-70b-versatile", gemini_model="gemini-2.0-flash"):
        self.groq_key,self.gemini_key,self.groq_model,self.gemini_model=groq_key,gemini_key,groq_model,gemini_model
    async def complete(self, messages, tools):
        async with httpx.AsyncClient(timeout=40) as client:
            if self.groq_key:
                try:
                    r=await _retry(client,"POST","https://api.groq.com/openai/v1/chat/completions",headers={"Authorization":f"Bearer {self.groq_key}"},json={"model":self.groq_model,"messages":messages,"tools":tools,"tool_choice":"auto","temperature":0.1}); return r.json()["choices"][0]["message"]
                except Exception: pass
            if self.gemini_key:
                prompt="\n".join(f"{m['role']}: {m.get('content','')}" for m in messages)
                r=await _retry(client,"POST",f"https://generativelanguage.googleapis.com/v1beta/models/{self.gemini_model}:generateContent?key={self.gemini_key}",json={"contents":[{"parts":[{"text":prompt}]}]}); return {"role":"assistant","content":r.json()["candidates"][0]["content"]["parts"][0]["text"]}
        return None
