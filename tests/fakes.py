from collections import deque

from langchain_core.messages import AIMessage

from app.schemas import RouteDecision, Source, SupportResponse

SOURCE = Source(
    id="document-requirements",
    title="Documentos requeridos",
    source="requirements.json",
    version="1",
    text="El caso puede esperar documentos. Domicilio es un comprobante legible y vigente.",
)


class FakeRetriever:
    async def search(self, query):
        return [SOURCE]

    async def ready(self):
        return None

    async def close(self):
        return None


class ScriptedModel:
    def __init__(self, routes, messages=(), responses=()):
        self.routes = deque(routes)
        self.messages = deque(messages)
        self.responses = deque(responses)
        self.inputs = []
        self.schema = None

    def with_structured_output(self, schema, **kwargs):
        self.schema = schema
        return self

    def bind_tools(self, tools, **kwargs):
        self.schema = None
        return self

    async def ainvoke(self, messages):
        self.inputs.append(messages)
        if self.schema is RouteDecision:
            return RouteDecision(next_agent=self.routes.popleft(), instruction="Resolver consulta")
        if self.schema is SupportResponse:
            return self.responses.popleft()
        return self.messages.popleft()


def call(name, **args):
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"call-{name}"}])


def knowledge_messages():
    return [call("buscar_documentacion", query="Documentos pendientes"), AIMessage("Ver fuente.")]


def case_messages(case_id="CASE-101"):
    return [call("consultar_caso", case_id=case_id), AIMessage("Caso consultado.")]


def case_response(**kwargs):
    return SupportResponse(
        status="answered",
        answer="El caso CASE-101 espera el comprobante de domicilio.",
        citations=[SOURCE.id],
        case_id="CASE-101",
        missing_documents=["domicilio"],
        **kwargs,
    )
