from typing import Any, Literal
from pydantic import BaseModel, Field

class Session(BaseModel):
    user_id: str
    role: Literal["support_agent", "ops_manager", "viewer"]
    allowed_account_ids: list[str] = Field(default_factory=list)
    all_accounts: bool = False
    session_id: str | None = None

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
    record_id: str | None = None
    include_related: bool = True
    query_scope: Literal["assigned_accounts", "other_accounts", "all_accounts"] = "assigned_accounts"

class AnalyticsQuery(BaseModel):
    analysis_type: Literal["recurring_ticket_issues"] = "recurring_ticket_issues"
    scope: Literal["assigned_accounts", "all_accounts"] = "all_accounts"
    min_accounts: int = Field(default=2, ge=2, le=10)
    include_closed: bool = True

class EvaluateQuery(BaseModel):
    order_id: str | None = None
    account_id: str | None = None
    customer_name: str | None = None
    evaluation_type: Literal["cancellation", "service_credit"]
    reported_pickup_at: str | None = None
    scenario_text: str | None = None
    booking_age_hours: float | None = Field(default=None, ge=0)
    delay_hours: float | None = Field(default=None, ge=0)
    carrier_fault: bool | None = None
    customer_fault: bool | None = None

class ProposalQuery(BaseModel):
    account_id: str
    order_id: str | None = None
    ticket_id: str | None = None
    reason: str
    severity: Literal["P1", "P2", "P3"]
    evidence_citation_ids: list[str] = Field(default_factory=list)
