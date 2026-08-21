from typing import Any, Literal
from pydantic import BaseModel, Field

class Session(BaseModel):
    user_id: str
    role: Literal["support_agent", "ops_manager", "viewer"]
    allowed_account_ids: list[str] = Field(default_factory=list)
    all_accounts: bool = False

class LoginRequest(BaseModel):
    user_id: str

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    turn_id: str | None = None

class ConfirmRequest(BaseModel):
    confirmed: bool

class DocumentQuery(BaseModel):
    query: str
    account_id: str | None = None
    topics: list[str] = Field(default_factory=list)
    include_context_only: bool = False

class LookupQuery(BaseModel):
    record_type: Literal["account", "order", "ticket"]
    record_id: str
    include_related: bool = True

class EvaluateQuery(BaseModel):
    order_id: str
    evaluation_type: Literal["cancellation", "service_credit"]
    reported_pickup_at: str | None = None

class ProposalQuery(BaseModel):
    account_id: str
    order_id: str | None = None
    ticket_id: str | None = None
    reason: str
    severity: Literal["P1", "P2", "P3"]
    evidence_citation_ids: list[str] = Field(default_factory=list)
