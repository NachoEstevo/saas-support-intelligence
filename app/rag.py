import asyncio
import hashlib
import json
import logging
import math
import re
import unicodedata
from pathlib import Path

import chromadb
import httpx
import tiktoken
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel, ConfigDict, Field, field_validator
from rank_bm25 import BM25Okapi

from app.config import BASE_DIR, Settings
from app.schemas import Source

logger = logging.getLogger(__name__)
CORPUS_DIR = BASE_DIR / "data" / "knowledge"
MANIFEST_VERSION = "support-corpus-v1"


class KnowledgeDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    title: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=1000000)

    @field_validator("title", "version", "text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Knowledge fields must not be blank")
        return value.strip()


def normalize(text: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(character for character in plain if not unicodedata.combining(character))
    return re.findall(r"[a-z0-9]+", plain)


def reciprocal_rank_fusion(lexical: list[str], vector: list[str], top_k: int) -> list[str]:
    scores: dict[str, float] = {}
    for ranking, weight in ((lexical, 0.4), (vector, 0.6)):
        for rank, chunk_id in enumerate(dict.fromkeys(ranking), start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (60 + rank)
    return sorted(scores, key=lambda key: (-scores[key], key))[:top_k]


def load_corpus(directory: Path) -> tuple[list[Source], str]:
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise ValueError("Knowledge corpus is empty")
    encoding = tiktoken.get_encoding("cl100k_base")
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=600,
        chunk_overlap=80,
        length_function=lambda text: len(encoding.encode(text, disallowed_special=())),
    )
    chunks: dict[str, Source] = {}
    manifest = []
    for path in paths:
        try:
            document = KnowledgeDocument.model_validate_json(path.read_text(encoding="utf-8"))
            manifest.append({"source": path.name, **document.model_dump()})
            for text in splitter.split_text(document.text):
                if len(encoding.encode(text, disallowed_special=())) > 600:
                    raise ValueError("Chunk exceeds token limit")
                payload = json.dumps([path.name, document.version, text], ensure_ascii=False)
                chunk_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                chunks[chunk_id] = Source(
                    id=chunk_id,
                    source=path.name,
                    title=document.title,
                    version=document.version,
                    text=text,
                )
        except (OSError, ValueError):
            logger.error("Invalid knowledge document: %s", path.name)
            raise ValueError(f"Invalid knowledge document: {path.name}") from None
    if not chunks:
        raise ValueError("Knowledge corpus is empty")
    fingerprint = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return list(chunks.values()), fingerprint


def lexical_ranking(chunks: list[Source], query: str) -> list[str]:
    tokens = [normalize(chunk.text) for chunk in chunks]
    if not any(tokens):
        return []
    scores = BM25Okapi(tokens).get_scores(normalize(query))
    matches = [
        (chunk.id, float(score)) for chunk, score in zip(chunks, scores, strict=True) if score > 0
    ]
    return [chunk_id for chunk_id, _ in sorted(matches, key=lambda item: (-item[1], item[0]))]


def chunk_metadata(chunk: Source) -> dict[str, str]:
    return {
        "title": chunk.title,
        "source": chunk.source,
        "version": chunk.version,
        "text": chunk.text,
        "category": Path(chunk.source).stem,
    }


class HybridRetriever:
    def __init__(
        self,
        settings: Settings,
        collection,
        embeddings,
        *,
        corpus_dir: Path = CORPUS_DIR,
        client=None,
        http_client: httpx.Client | None = None,
        http_async_client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self.collection = collection
        self.embeddings = embeddings
        self.corpus_dir = corpus_dir
        self.client = client
        self.http_client = http_client
        self.http_async_client = http_async_client
        self._lock = asyncio.Lock()

    @staticmethod
    def initial_metadata(settings: Settings) -> dict[str, str | int]:
        return {
            "manifest_version": MANIFEST_VERSION,
            "embedding_model": settings.embedding_model,
            "embedding_dimension": settings.embedding_dimension,
            "corpus_fingerprint": "",
            "indexed": 0,
            "known_ids": "[]",
            "pending_ids": "[]",
        }

    @classmethod
    async def connect(cls, settings: Settings) -> "HybridRetriever":
        client = await chromadb.AsyncHttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port,
            settings=chromadb.Settings(anonymized_telemetry=False),
        )
        collection = await client.get_or_create_collection(
            name=settings.chroma_collection,
            embedding_function=None,
            configuration={"hnsw": {"space": "cosine"}},
            metadata=cls.initial_metadata(settings),
        )
        if collection.configuration.get("hnsw", {}).get("space") != "cosine":
            raise ValueError("Collection must use cosine distance")
        sync_http = httpx.Client(timeout=30)
        async_http = httpx.AsyncClient(timeout=30)
        try:
            embeddings = OpenAIEmbeddings(
                model=settings.embedding_model,
                dimensions=settings.embedding_dimension,
                api_key=settings.openai_api_key.get_secret_value(),
                http_client=sync_http,
                http_async_client=async_http,
                max_retries=2,
                request_timeout=30,
                check_embedding_ctx_length=False,
            )
            retriever = cls(
                settings,
                collection,
                embeddings,
                client=client,
                http_client=sync_http,
                http_async_client=async_http,
            )
            retriever._manifest()
            return retriever
        except Exception:
            await async_http.aclose()
            sync_http.close()
            raise

    async def _refresh_collection(self) -> None:
        if self.client is not None:
            self.collection = await self.client.get_collection(
                self.settings.chroma_collection, embedding_function=None
            )

    def _manifest(self) -> tuple[dict, set[str], set[str]]:
        metadata = self.collection.metadata or {}
        if metadata.get("manifest_version") != MANIFEST_VERSION:
            raise ValueError("Unmanifested collection: ingestion refused")
        if (
            metadata.get("embedding_model") != self.settings.embedding_model
            or metadata.get("embedding_dimension") != self.settings.embedding_dimension
        ):
            raise ValueError("Embedding configuration mismatch")
        try:
            known = json.loads(metadata["known_ids"])
            pending = json.loads(metadata.get("pending_ids", "[]"))
            if (
                any(
                    not isinstance(ids, list)
                    or any(
                        not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{64}", item)
                        for item in ids
                    )
                    or len(ids) != len(set(ids))
                    for ids in (known, pending)
                )
                or metadata.get("indexed") not in (0, 1)
                or not isinstance(metadata.get("corpus_fingerprint"), str)
                or (
                    metadata.get("indexed") == 1
                    and not re.fullmatch(r"[a-f0-9]{64}", metadata["corpus_fingerprint"])
                )
            ):
                raise ValueError("Invalid manifest")
        except (KeyError, TypeError, ValueError):
            raise ValueError("Invalid corpus manifest") from None
        return metadata, set(known), set(pending)

    def _validate_embeddings(self, vectors: list[list[float]], expected: int) -> None:
        if len(vectors) != expected or any(
            len(vector) != self.settings.embedding_dimension
            or any(not math.isfinite(value) for value in vector)
            for vector in vectors
        ):
            raise ValueError("Embedding provider returned invalid dimensions or values")

    async def ingest(self) -> dict[str, int]:
        async with self._lock:
            chunks, fingerprint = await asyncio.to_thread(load_corpus, self.corpus_dir)
            await self._refresh_collection()
            metadata, known_ids, pending_ids = self._manifest()
            owned_ids = known_ids | pending_ids
            if not metadata["indexed"]:
                stored = await self.collection.get(include=[])
                if set(stored["ids"]) - owned_ids:
                    raise ValueError("Unmanifested collection contents: ingestion refused")
            current = {chunk.id: chunk for chunk in chunks}
            existing = await self.collection.get(ids=list(current), include=["metadatas"])
            existing_ids = set(existing["ids"]) & owned_ids
            stored_metadata = dict(zip(existing["ids"], existing["metadatas"], strict=True))
            changed = [chunk for chunk in chunks if chunk.id not in existing_ids]
            if changed:
                vectors = await self.embeddings.aembed_documents([chunk.text for chunk in changed])
                self._validate_embeddings(vectors, len(changed))
                metadata = {
                    **metadata,
                    "pending_ids": json.dumps(
                        sorted(pending_ids | {chunk.id for chunk in changed})
                    ),
                }
                await self.collection.modify(metadata=metadata)
                await self.collection.upsert(
                    ids=[chunk.id for chunk in changed],
                    embeddings=vectors,
                    documents=[chunk.text for chunk in changed],
                    metadatas=[chunk_metadata(chunk) for chunk in changed],
                )
            metadata_only = [
                chunk
                for chunk in chunks
                if chunk.id in existing_ids and stored_metadata[chunk.id] != chunk_metadata(chunk)
            ]
            if metadata_only:
                await self.collection.update(
                    ids=[chunk.id for chunk in metadata_only],
                    metadatas=[chunk_metadata(chunk) for chunk in metadata_only],
                )
            obsolete = owned_ids - current.keys()
            if obsolete:
                await self.collection.delete(ids=sorted(obsolete))
            updated_metadata = {
                **self.initial_metadata(self.settings),
                "indexed": 1,
                "corpus_fingerprint": fingerprint,
                "known_ids": json.dumps(sorted(current)),
            }
            if metadata != updated_metadata:
                await self.collection.modify(metadata=updated_metadata)
            return {
                "inserted": len(changed),
                "skipped": len(chunks) - len(changed),
                "deleted": len(obsolete),
            }

    async def _validated_chunks(self) -> list[Source]:
        chunks, fingerprint = await asyncio.to_thread(load_corpus, self.corpus_dir)
        await self._refresh_collection()
        metadata, known_ids, pending_ids = self._manifest()
        if not metadata["indexed"]:
            raise ValueError("Knowledge corpus is not indexed")
        if pending_ids:
            raise ValueError("Knowledge ingestion is pending; run ingestion")
        if metadata["corpus_fingerprint"] != fingerprint:
            raise ValueError("Indexed corpus does not match local corpus; run ingestion")
        if known_ids != {chunk.id for chunk in chunks}:
            raise ValueError("Indexed chunks do not match local corpus; run ingestion")
        existing = await self.collection.get(ids=sorted(known_ids), include=[])
        if set(existing["ids"]) != known_ids:
            raise ValueError("Indexed chunks are missing; run ingestion")
        return chunks

    async def ready(self) -> None:
        async with self._lock:
            await self._validated_chunks()

    async def search(self, query: str) -> list[Source]:
        if not query.strip():
            return []
        async with self._lock:
            chunks = await self._validated_chunks()
            current = {chunk.id: chunk for chunk in chunks}
            lexical = await asyncio.to_thread(lexical_ranking, chunks, query)
            vector = await self.embeddings.aembed_query(query)
            self._validate_embeddings([vector], 1)
            result = await self.collection.query(
                query_embeddings=[vector],
                ids=sorted(current),
                n_results=min(len(chunks), max(self.settings.top_k * 4, 10)),
                include=["distances"],
            )
            vector_ids = [chunk_id for chunk_id in result["ids"][0] if chunk_id in current]
            fused = reciprocal_rank_fusion(lexical, vector_ids, self.settings.top_k)
            return [current[chunk_id] for chunk_id in fused]

    async def close(self) -> None:
        if self.http_async_client is not None:
            await self.http_async_client.aclose()
        if self.http_client is not None:
            await asyncio.to_thread(self.http_client.close)
