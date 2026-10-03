import json
from pathlib import Path

import pytest

from app.config import Settings
from app.rag import CORPUS_DIR, HybridRetriever, load_corpus, normalize, reciprocal_rank_fusion
from app.schemas import Source, SupportCase


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        openai_api_key="not-a-provider-key",
        llm_model="test-model",
        embedding_model="test-embedding",
        embedding_dimension=3,
        customer_a_key="customer-a-test-key",
        customer_b_key="customer-b-test-key",
        approver_a_key="approver-a-test-key",
        approver_b_key="approver-b-test-key",
        top_k=2,
    )


def write_doc(directory, name="requirements", text="Documento domicilio e identidad necesarios"):
    directory.mkdir(exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"title": name, "version": "1", "text": text}), encoding="utf-8")
    return path


class FakeCollection:
    def __init__(self, settings):
        self.metadata = HybridRetriever.initial_metadata(settings)
        self.rows = {}
        self.fail_upsert = False
        self.fail_final_modify_once = False
        self.vector_ids = []

    async def get(self, ids=None, include=None):
        keys = [key for key in (ids or self.rows) if key in self.rows]
        return {"ids": keys, "embeddings": None, "metadatas": [self.rows[key][1] for key in keys]}

    async def count(self):
        return len(self.rows)

    async def upsert(self, ids, embeddings, metadatas, documents):
        if self.fail_upsert:
            raise RuntimeError("upsert unavailable")
        for key, embedding, metadata, document in zip(
            ids, embeddings, metadatas, documents, strict=True
        ):
            self.rows[key] = (embedding, metadata, document)

    async def update(self, ids, metadatas):
        for key, metadata in zip(ids, metadatas, strict=True):
            embedding, _, document = self.rows[key]
            self.rows[key] = (embedding, metadata, document)

    async def delete(self, ids):
        for key in ids:
            self.rows.pop(key, None)

    async def modify(self, metadata):
        if (
            self.fail_final_modify_once
            and metadata["indexed"] == 1
            and metadata.get("pending_ids", "[]") == "[]"
        ):
            self.fail_final_modify_once = False
            raise RuntimeError("manifest commit unavailable")
        self.metadata = metadata

    async def query(self, query_embeddings, n_results, include, ids=None):
        ranked = self.vector_ids or list(self.rows)
        allowed = set(ids) if ids is not None else None
        return {"ids": [[key for key in ranked if allowed is None or key in allowed][:n_results]]}


class FakeEmbeddings:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def aembed_documents(self, texts):
        self.calls.append(texts)
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def aembed_query(self, text):
        return [1.0, 0.0, 0.0]


@pytest.fixture
def retriever(settings, tmp_path):
    write_doc(tmp_path)
    collection = FakeCollection(settings)
    embeddings = FakeEmbeddings()
    return HybridRetriever(settings, collection, embeddings, corpus_dir=tmp_path)


def test_token_limit_and_stable_ids(tmp_path):
    import tiktoken

    write_doc(tmp_path, text="Identidad, domicilio y revisión operativa. " * 1600)
    first, fingerprint = load_corpus(tmp_path)
    second, repeated = load_corpus(tmp_path)
    encoding = tiktoken.get_encoding("cl100k_base")
    assert len(first) > 1
    assert max(len(encoding.encode(chunk.text)) for chunk in first) <= 600
    assert first == second
    assert fingerprint == repeated
    assert len({chunk.id for chunk in first}) == len(first)


def test_normalization_and_weighted_rrf():
    assert normalize("REVISIÓN, Identidad: Acción") == ["revision", "identidad", "accion"]
    ranked = reciprocal_rank_fusion(["a", "b"], ["b", "c", "b"], 3)
    assert ranked == ["b", "c", "a"]


@pytest.mark.parametrize("content", ["{", "{}", '{"title":"a","version":"1","text":" "}'])
def test_corrupt_document_fails_without_content_in_error(tmp_path, content, caplog):
    (tmp_path / "corrupt.json").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid knowledge document: corrupt.json"):
        load_corpus(tmp_path)
    assert content not in caplog.text


def test_empty_corpus_fails(tmp_path):
    with pytest.raises(ValueError, match="empty"):
        load_corpus(tmp_path)


@pytest.mark.asyncio
async def test_unchanged_ingestion_skips_embeddings(retriever):
    assert await retriever.ingest() == {"inserted": 1, "skipped": 0, "deleted": 0}
    assert await retriever.ingest() == {"inserted": 0, "skipped": 1, "deleted": 0}
    assert len(retriever.embeddings.calls) == 1


@pytest.mark.asyncio
async def test_changed_and_deleted_documents(retriever):
    extra = write_doc(retriever.corpus_dir, "status", "Estado operativo pendiente de revisión")
    await retriever.ingest()
    old_ids = set(retriever.collection.rows)
    extra.unlink()
    write_doc(retriever.corpus_dir, text="Domicilio e identidad completos requieren revisión")
    assert await retriever.ingest() == {"inserted": 1, "skipped": 0, "deleted": 2}
    assert not set(retriever.collection.rows) & old_ids
    assert len(retriever.embeddings.calls[-1]) == 1


@pytest.mark.asyncio
async def test_failed_upsert_does_not_delete_or_advance_manifest(retriever):
    await retriever.ingest()
    old_ids = set(retriever.collection.rows)
    old_manifest = retriever.collection.metadata.copy()
    write_doc(retriever.corpus_dir, text="Domicilio actualizado")
    retriever.collection.fail_upsert = True
    with pytest.raises(RuntimeError):
        await retriever.ingest()
    assert set(retriever.collection.rows) == old_ids
    assert {
        key: value for key, value in retriever.collection.metadata.items() if key != "pending_ids"
    } == {key: value for key, value in old_manifest.items() if key != "pending_ids"}
    assert retriever.collection.metadata["pending_ids"] != "[]"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("embedding_model", "other"), ("embedding_dimension", 9)])
async def test_model_mismatch_fails_closed(retriever, field, value):
    retriever.collection.metadata[field] = value
    with pytest.raises(ValueError, match="Embedding configuration mismatch"):
        await retriever.ingest()
    assert not retriever.embeddings.calls


@pytest.mark.asyncio
async def test_search_requires_current_index(retriever):
    with pytest.raises(ValueError, match="not indexed"):
        await retriever.search("domicilio")
    await retriever.ingest()
    write_doc(retriever.corpus_dir, text="Nuevo domicilio")
    with pytest.raises(ValueError, match="does not match"):
        await retriever.search("domicilio")


@pytest.mark.asyncio
async def test_top_k_dedupes_chunks_and_ignores_unknown_ids(retriever):
    write_doc(retriever.corpus_dir, "status", "Domicilio pendiente de revisión")
    write_doc(retriever.corpus_dir, "upload", "Carga PDF PNG JPG")
    await retriever.ingest()
    known = list(retriever.collection.rows)
    retriever.collection.vector_ids = ["unknown", known[1], known[1], known[0], known[2]]
    sources = await retriever.search("domicilio")
    assert len(sources) == 2
    assert len({source.id for source in sources}) == 2
    assert all(isinstance(source, Source) and source.id in known for source in sources)


@pytest.mark.asyncio
async def test_lexical_ranking_excludes_zero_matches(retriever):
    await retriever.ingest()
    retriever.collection.vector_ids = ["unknown"]
    assert await retriever.search("zzzzzz") == []


@pytest.mark.asyncio
async def test_unmanifested_nonempty_collection_is_rejected(retriever):
    retriever.collection.rows["unknown"] = ([], {}, "untrusted")
    with pytest.raises(ValueError, match="Unmanifested"):
        await retriever.ingest()


@pytest.mark.asyncio
async def test_missing_known_chunk_is_repaired(retriever):
    await retriever.ingest()
    retriever.collection.rows.clear()
    assert await retriever.ingest() == {"inserted": 1, "skipped": 0, "deleted": 0}


@pytest.mark.asyncio
async def test_title_only_update_preserves_embeddings_and_refreshes_metadata(retriever):
    await retriever.ingest()
    path = retriever.corpus_dir / "requirements.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["title"] = "Título actualizado"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert await retriever.ingest() == {"inserted": 0, "skipped": 1, "deleted": 0}
    assert len(retriever.embeddings.calls) == 1
    assert next(iter(retriever.collection.rows.values()))[1]["title"] == "Título actualizado"
    assert (await retriever.search("domicilio"))[0].title == "Título actualizado"


@pytest.mark.asyncio
async def test_failed_embedding_does_not_delete_or_advance_manifest(retriever):
    await retriever.ingest()
    old_ids = set(retriever.collection.rows)
    old_manifest = retriever.collection.metadata.copy()
    write_doc(retriever.corpus_dir, text="Domicilio actualizado")
    retriever.embeddings.fail = True
    with pytest.raises(RuntimeError):
        await retriever.ingest()
    assert set(retriever.collection.rows) == old_ids
    assert retriever.collection.metadata == old_manifest


@pytest.mark.asyncio
async def test_unknown_collection_rows_survive_updates(retriever):
    await retriever.ingest()
    retriever.collection.rows["unknown"] = ([], {}, "untrusted")
    write_doc(retriever.corpus_dir, text="Domicilio actualizado")
    assert (await retriever.ingest())["deleted"] == 1
    assert "unknown" in retriever.collection.rows
    assert all(source.id != "unknown" for source in await retriever.search("domicilio"))


@pytest.mark.asyncio
async def test_embedding_dimension_rejected(retriever):
    async def invalid_embeddings(texts):
        return [[1.0] for text in texts]

    retriever.embeddings.aembed_documents = invalid_embeddings
    with pytest.raises(ValueError, match="invalid dimensions"):
        await retriever.ingest()
    assert not retriever.collection.rows


@pytest.mark.asyncio
async def test_close_owned_embedding_sessions(retriever):
    import httpx

    retriever.http_client = httpx.Client()
    retriever.http_async_client = httpx.AsyncClient()
    await retriever.close()
    assert retriever.http_client.is_closed
    assert retriever.http_async_client.is_closed


@pytest.mark.asyncio
async def test_connect_uses_manual_embeddings_and_cosine(settings, monkeypatch):
    import app.rag as rag

    collection = FakeCollection(settings)
    collection.configuration = {"hnsw": {"space": "cosine"}}
    calls = {}

    class FakeClient:
        async def get_or_create_collection(self, **kwargs):
            calls["collection"] = kwargs
            return collection

    async def fake_http_client(**kwargs):
        calls["http"] = kwargs
        return FakeClient()

    monkeypatch.setattr(rag.chromadb, "AsyncHttpClient", fake_http_client)
    retriever = await HybridRetriever.connect(settings)
    try:
        assert calls["collection"]["embedding_function"] is None
        assert calls["collection"]["configuration"] == {"hnsw": {"space": "cosine"}}
        assert calls["collection"]["metadata"]["indexed"] == 0
        assert retriever.embeddings.model == settings.embedding_model
        assert retriever.embeddings.dimensions == settings.embedding_dimension
    finally:
        await retriever.close()


@pytest.mark.asyncio
async def test_same_source_multiple_chunks_are_not_collapsed(retriever):
    text = "\n\n".join(
        f"Documento número {number}. Domicilio identidad pendiente de revisión. " * 20
        for number in range(10)
    )
    write_doc(retriever.corpus_dir, text=text)
    await retriever.ingest()
    sources = await retriever.search("domicilio")
    assert len(sources) == 2
    assert len({source.source for source in sources}) == 1
    assert len({source.id for source in sources}) == 2


@pytest.mark.asyncio
async def test_deleted_last_document_refuses_ingestion_without_deleting_index(retriever):
    await retriever.ingest()
    old_ids = set(retriever.collection.rows)
    (retriever.corpus_dir / "requirements.json").unlink()
    with pytest.raises(ValueError, match="empty"):
        await retriever.ingest()
    assert set(retriever.collection.rows) == old_ids


@pytest.mark.asyncio
async def test_ready_validates_without_paid_calls_or_query(retriever):
    with pytest.raises(ValueError, match="not indexed"):
        await retriever.ready()
    await retriever.ingest()

    async def unexpected_call(*args, **kwargs):
        pytest.fail("readiness must not embed or query")

    retriever.embeddings.aembed_query = unexpected_call
    retriever.embeddings.aembed_documents = unexpected_call
    retriever.collection.query = unexpected_call
    assert await retriever.ready() is None
    retriever.collection.rows.clear()
    with pytest.raises(ValueError, match="missing"):
        await retriever.ready()


@pytest.mark.asyncio
async def test_ready_rejects_stale_corpus(retriever):
    await retriever.ingest()
    write_doc(retriever.corpus_dir, text="Nuevo domicilio")
    with pytest.raises(ValueError, match="does not match"):
        await retriever.ready()


@pytest.mark.asyncio
async def test_initial_manifest_commit_failure_recovers_owned_upserts(retriever):
    for number in range(8):
        write_doc(retriever.corpus_dir, f"extra-{number}", f"Guía global número {number}")
    retriever.collection.fail_final_modify_once = True
    with pytest.raises(RuntimeError, match="manifest commit unavailable"):
        await retriever.ingest()
    assert len(retriever.collection.rows) == 9
    with pytest.raises(ValueError, match="not indexed"):
        await retriever.ready()
    assert await retriever.ingest() == {"inserted": 0, "skipped": 9, "deleted": 0}
    assert len(retriever.embeddings.calls) == 1
    assert await retriever.ready() is None


@pytest.mark.asyncio
async def test_pending_rows_are_reconciled_when_corpus_changes_during_recovery(retriever):
    retriever.collection.fail_final_modify_once = True
    with pytest.raises(RuntimeError, match="manifest commit unavailable"):
        await retriever.ingest()
    previous_ids = set(retriever.collection.rows)
    write_doc(retriever.corpus_dir, text="Requisito domicilio actualizado")
    assert await retriever.ingest() == {"inserted": 1, "skipped": 0, "deleted": 1}
    assert not previous_ids & set(retriever.collection.rows)
    assert retriever.collection.metadata["pending_ids"] == "[]"
    assert await retriever.ready() is None


@pytest.mark.asyncio
async def test_pending_bootstrap_does_not_claim_foreign_rows(retriever):
    retriever.collection.fail_final_modify_once = True
    with pytest.raises(RuntimeError, match="manifest commit unavailable"):
        await retriever.ingest()
    retriever.collection.rows["foreign"] = ([], {}, "unowned")
    previous_rows = retriever.collection.rows.copy()
    with pytest.raises(ValueError, match="Unmanifested"):
        await retriever.ingest()
    assert retriever.collection.rows == previous_rows
    assert len(retriever.embeddings.calls) == 1


@pytest.mark.asyncio
async def test_foreign_vector_rows_cannot_consume_candidate_window(retriever):
    await retriever.ingest()
    known_ids = list(retriever.collection.rows)
    retriever.collection.vector_ids = [f"foreign-{number}" for number in range(20)] + known_ids
    sources = await retriever.search("zzzzzz")
    assert [source.id for source in sources] == known_ids


def test_global_corpus_separates_public_guides_from_synthetic_policies_and_case_data():
    chunks, _ = load_corpus(CORPUS_DIR)
    assert len({chunk.source for chunk in chunks if chunk.kind == "synthetic"}) == 9
    assert len({chunk.source for chunk in chunks if chunk.kind == "public"}) == 3
    text = " ".join(chunk.text for chunk in chunks)
    assert "CASE-101" not in text
    assert "Alba Demo" not in text
    assert "Beta Demo" not in text
    assert {"document-requirements.json", "case-status.json"} <= {chunk.source for chunk in chunks}


def test_seed_cases_are_scoped_and_valid():
    raw = json.loads((Path(__file__).resolve().parents[1] / "data/cases.json").read_text())
    cases = [SupportCase.model_validate(value) for value in raw]
    assert [(case.tenant_id, case.case_id) for case in cases] == [
        ("demo-a", "CASE-101"),
        ("demo-a", "CASE-102"),
        ("demo-b", "CASE-201"),
    ]
    assert cases[0].missing_documents == ["domicilio"]
    assert cases[1].missing_documents == []
    assert cases[2].required_documents != cases[0].required_documents
