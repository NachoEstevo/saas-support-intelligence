import asyncio
import json
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from app.schemas import CaseInput, CaseResult, SupportCase, TicketDraft
from app.store import NotFound, Store


class SupportService:
    def __init__(self, store: Store):
        self.store = store

    async def seed(self, path: Path) -> None:
        raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
        cases = [SupportCase.model_validate(item) for item in json.loads(raw)]
        if len({(case.tenant_id, case.case_id) for case in cases}) != len(cases):
            raise ValueError("Duplicate demo cases")
        await self.store.seed_cases(cases)

    def tools(self, tenant_id: str) -> list[BaseTool]:
        @tool(args_schema=CaseInput)
        async def consultar_caso(case_id: str) -> dict:
            """Consulta un caso de la cuenta autenticada y sus documentos pendientes.

            Necesitás su código CASE-NNN. Devuelve estado, documentos requeridos
            y cargados. No expone otras cuentas ni permite modificar un trámite.
            """
            case = await self.store.get_case(tenant_id, case_id)
            return CaseResult(
                found=case is not None,
                case=case,
                missing_documents=case.missing_documents if case else [],
            ).model_dump(mode="json")

        @tool(args_schema=TicketDraft)
        async def preparar_ticket(
            subject: str,
            description: str,
            case_id: str | None = None,
        ) -> dict:
            """Prepara un borrador de ticket sin crearlo ni enviarlo.

            Incluí el problema, hechos verificados e intervención necesaria.
            Solo vinculá casos de la cuenta autenticada. Un aprobador debe
            autorizar el borrador antes de guardar el ticket local.
            """
            if case_id and await self.store.get_case(tenant_id, case_id) is None:
                raise NotFound("Case not found")
            draft = TicketDraft(subject=subject, description=description, case_id=case_id)
            return {"ticket": draft.model_dump(mode="json")}

        return [consultar_caso, preparar_ticket]
