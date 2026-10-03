import os
from pathlib import Path

from langchain_openai import ChatOpenAI
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", extra="ignore", hide_input_in_errors=True
    )
    openai_api_key: SecretStr
    llm_model: str
    embedding_model: str
    embedding_dimension: int = 1536
    langsmith_api_key: SecretStr | None = None
    langsmith_tracing: bool = False
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_project: str = "saas-support-intelligence"
    customer_a_key: SecretStr
    customer_b_key: SecretStr
    approver_a_key: SecretStr
    approver_b_key: SecretStr
    redis_url: str = "redis://127.0.0.1:16389/0"
    redis_prefix: str = "saas-support"
    chroma_host: str = "127.0.0.1"
    chroma_port: int = 18081
    chroma_collection: str = "support-knowledge"
    worker_concurrency: int = 3
    job_timeout_seconds: int = 180
    top_k: int = 4

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        keys = [
            self.customer_a_key,
            self.customer_b_key,
            self.approver_a_key,
            self.approver_b_key,
        ]
        raw = [key.get_secret_value() for key in keys]
        if not self.openai_api_key.get_secret_value().strip():
            raise ValueError("OpenAI credential is required")
        if not self.llm_model.strip() or not self.embedding_model.strip():
            raise ValueError("Model names cannot be blank")
        if len(set(raw)) != 4 or any(len(key) < 16 for key in raw):
            raise ValueError("Four distinct API credentials of at least 16 characters are required")
        if not 1 <= self.worker_concurrency <= 5 or not 10 <= self.job_timeout_seconds <= 600:
            raise ValueError("Worker settings are outside supported limits")
        if not 1 <= self.top_k <= 5 or not 1 <= self.embedding_dimension <= 3072:
            raise ValueError("Retrieval settings are outside supported limits")
        if self.langsmith_tracing and (
            not self.langsmith_api_key or not self.langsmith_api_key.get_secret_value().strip()
        ):
            raise ValueError("Tracing requires a LangSmith credential")
        return self

    def configure_tracing(self) -> None:
        os.environ["LANGSMITH_TRACING"] = str(self.langsmith_tracing).lower()
        os.environ["LANGSMITH_ENDPOINT"] = self.langsmith_endpoint
        os.environ["LANGSMITH_PROJECT"] = self.langsmith_project
        if self.langsmith_api_key:
            os.environ["LANGSMITH_API_KEY"] = self.langsmith_api_key.get_secret_value()

    def create_model(self) -> ChatOpenAI:
        return ChatOpenAI(
            model=self.llm_model,
            api_key=self.openai_api_key.get_secret_value(),
            use_responses_api=True,
            reasoning={"effort": "low"},
            timeout=30,
            max_retries=2,
            max_tokens=2000,
        )
