from typing import Literal
from uuid import UUID

from langgraph.graph import MessagesState
from pydantic import Field

from app.schemas import CaseResult, Contract, Source, SupportResponse, TicketDraft


class ExecutionContext(Contract):
    tenant_id: str
    job_id: UUID


class Contribution(Contract):
    agent: Literal["knowledge", "operations"]
    narrative: str
    sources: list[Source] = Field(default_factory=list)
    case_result: CaseResult | None = None
    ticket: TicketDraft | None = None
    events: list[dict[str, str | float]] = Field(default_factory=list)


class SupportState(MessagesState):
    query: str
    in_scope: bool
    next_agent: Literal["knowledge", "operations", "synthesis"]
    instruction: str
    contributions: dict[str, Contribution]
    sources: list[Source]
    case_result: CaseResult | None
    last_case_id: str | None
    ticket_draft: TicketDraft | None
    response: SupportResponse | None
    feedback: str
    decisions: int
    refinements: int
    valid: bool
    rejected: bool
    ticket_id: UUID | None
    events: list[dict[str, str | int | float]]
