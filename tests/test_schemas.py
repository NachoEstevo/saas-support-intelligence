import pytest
from pydantic import ValidationError

from app.schemas import ApprovalRequest, MessageRequest, SupportCase, SupportResponse


@pytest.mark.parametrize("message", ["", "   ", "a" * 4001])
def test_rejects_empty_and_oversized_messages(message):
    with pytest.raises(ValidationError):
        MessageRequest(message=message)


def test_client_cannot_choose_identity():
    with pytest.raises(ValidationError):
        MessageRequest(message="Hola", tenant_id="other")


def test_missing_documents_are_computed_from_records():
    case = SupportCase(
        tenant_id="demo-a",
        case_id="CASE-101",
        company="Empresa demo",
        status="waiting_documents",
        required_documents=["identidad", "domicilio"],
        uploaded_documents=["identidad"],
    )
    assert case.missing_documents == ["domicilio"]


def test_approval_does_not_coerce_strings():
    with pytest.raises(ValidationError):
        ApprovalRequest(approved="true")


def test_response_rejects_unexpected_fields():
    with pytest.raises(ValidationError):
        SupportResponse(status="answered", answer="Respuesta", hidden_reasoning="x")
