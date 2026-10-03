import json

import pytest
from pydantic import ValidationError

from app.rag import KnowledgeDocument, chunk_metadata, load_corpus
from app.schemas import Source


def test_public_sources_keep_provenance_through_chunking(tmp_path):
    document = {
        "title": "Rely: plataforma pública",
        "version": "2026-10-03",
        "text": "Los documentos y su estado se muestran en la plataforma.",
        "kind": "public",
        "source_url": "https://www.rely.business/",
        "checked_at": "2026-10-03",
    }
    (tmp_path / "rely.json").write_text(json.dumps(document), encoding="utf-8")
    chunks, fingerprint = load_corpus(tmp_path)
    source = chunks[0]
    assert source.kind == "public"
    assert source.source_url == document["source_url"]
    assert source.checked_at == document["checked_at"]
    assert chunk_metadata(source)["kind"] == "public"
    assert chunk_metadata(source)["source_url"] == document["source_url"]
    document["checked_at"] = "2026-10-04"
    (tmp_path / "rely.json").write_text(json.dumps(document), encoding="utf-8")
    refreshed, new_fingerprint = load_corpus(tmp_path)
    assert refreshed[0].id == source.id
    assert new_fingerprint != fingerprint


@pytest.mark.parametrize(
    "changes",
    [
        {"source_url": "javascript:alert(1)"},
        {"checked_at": "2026-13-35"},
        {"source_url": None},
        {"checked_at": None},
    ],
)
def test_public_document_requires_safe_url_and_real_date(changes):
    values = {
        "title": "Rely",
        "version": "1",
        "text": "Fuente pública.",
        "kind": "public",
        "source_url": "https://www.rely.business/",
        "checked_at": "2026-10-03",
    }
    with pytest.raises(ValidationError):
        KnowledgeDocument.model_validate(values | changes)


def test_old_sources_remain_explicitly_synthetic():
    source = Source(id="abc", title="Demo", source="demo.json", version="1", text="Texto demo")
    assert source.kind == "synthetic"
    assert source.source_url is None
