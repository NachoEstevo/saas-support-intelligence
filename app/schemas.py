from datetime import date
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Principal(Contract):
    tenant_id: str
    role: Literal["customer", "approver"]


class Provenance(Contract):
    kind: Literal["synthetic", "public"] = "synthetic"
    source_url: str | None = Field(default=None, max_length=500)
    checked_at: str | None = None

    @field_validator("source_url")
    @classmethod
    def public_url(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("Source URL must be HTTPS without credentials")
        return value

    @field_validator("checked_at")
    @classmethod
    def checked_date(cls, value: str | None) -> str | None:
        if value is not None:
            return date.fromisoformat(value).isoformat()
        return value

    @model_validator(mode="after")
    def public_source(self) -> "Provenance":
        if self.kind == "public" and (not self.source_url or not self.checked_at):
            raise ValueError("Public sources require a URL and consultation date")
        return self


class Source(Provenance):
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=12000)


class SupportCase(Contract):
    tenant_id: str
    case_id: str = Field(pattern=r"^CASE-[0-9]{3}$")
    company: str
    status: Literal["waiting_documents", "in_review", "completed"]
    required_documents: list[str] = Field(max_length=10)
    uploaded_documents: list[str] = Field(max_length=10)

    @property
    def missing_documents(self) -> list[str]:
        return sorted(set(self.required_documents) - set(self.uploaded_documents))


class TicketDraft(Contract):
    subject: str = Field(min_length=3, max_length=150)
    description: str = Field(min_length=10, max_length=2000)
    case_id: str | None = Field(default=None, pattern=r"^CASE-[0-9]{3}$")


class Ticket(TicketDraft):
    id: UUID
    tenant_id: str


class SupportResponse(Contract):
    status: Literal["answered", "needs_information", "handoff"]
    answer: str = Field(min_length=1, max_length=4000)
    citations: list[str] = Field(default_factory=list, max_length=5)
    case_id: str | None = Field(default=None, pattern=r"^CASE-[0-9]{3}$")
    missing_documents: list[str] = Field(default_factory=list, max_length=10)
    ticket: TicketDraft | None = None


class MessageRequest(Contract):
    message: str = Field(min_length=1, max_length=4000)

    @field_validator("message", mode="before")
    @classmethod
    def strip_message(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ApprovalRequest(Contract):
    approved: bool = Field(strict=True)


class Conversation(Contract):
    id: UUID
    tenant_id: str


JobStatus = Literal["PENDING", "RUNNING", "WAITING_APPROVAL", "DONE", "FAILED", "REJECTED"]


class ConversationSummary(Conversation):
    title: str
    updated_at: float
    last_job_status: JobStatus | None = None


class Job(Contract):
    id: UUID
    conversation_id: UUID
    tenant_id: str
    message: str
    status: JobStatus = "PENDING"
    response: SupportResponse | None = None
    draft_answer: str = Field(default="", max_length=4000)
    sources: list[Source] = Field(default_factory=list)
    events: list[dict[str, str | int | float]] = Field(default_factory=list)
    trace_id: UUID | None = None
    ticket_id: UUID | None = None
    error: str | None = None
    resume: bool = False
    approved: bool | None = None


class AcceptedJob(Contract):
    id: UUID
    conversation_id: UUID
    status: JobStatus


class Health(Contract):
    status: Literal["ok"] = "ok"


class RouteDecision(Contract):
    next_agent: Literal["knowledge", "operations", "synthesis"]
    instruction: str = Field(min_length=1, max_length=1500)


class SearchInput(Contract):
    query: str = Field(min_length=1, max_length=1000)


class CaseInput(Contract):
    case_id: str = Field(pattern=r"^CASE-[0-9]{3}$")


class CaseResult(Contract):
    found: bool
    case: SupportCase | None = None
    missing_documents: list[str] = Field(default_factory=list)


class SearchResult(Contract):
    sources: list[Source]
