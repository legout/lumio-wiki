from __future__ import annotations

import importlib
import importlib.util
import re
from pathlib import Path

import pytest
from lumio_wiki.embeddings import EmbeddingError
from lumio_wiki.page_search import search_pages
from lumio_wiki.records import (
    CLAIM_STATUS_ACCEPTED,
    Claim,
    CompiledPage,
    Relationship,
    RetrievalResult,
    Source,
)
from lumio_wiki.retrieval import ZeroIndexRetrieval


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _claims_from(source_title: str, relationships: list[Relationship]) -> list[Claim]:
    """Convert title-level test edges to accepted entity-to-entity Claims."""
    return [
        Claim(
            id=f"claim:{_slug(source_title)}-{_slug(rel.target)}-{n}",
            predicate=rel.type,
            object=f"entity:{_slug(rel.target)}",
            status=CLAIM_STATUS_ACCEPTED,
        )
        for n, rel in enumerate(relationships)
    ]


def _pages() -> list[CompiledPage]:
    return [
        CompiledPage(
            path="overview.md",
            title="Lumio Overview",
            id="entity:lumio-overview",
            entity_types=["concept"],
            aliases=["Overview"],
            tags=["lumio"],
            summary="Portable trusted knowledge.",
            lifecycle="approved",
            visibility="public",
            sources=[Source(id="overview", title="Overview source")],
            claims=_claims_from(
                "Lumio Overview", [Relationship(type="uses", target="Technology Stack")]
            ),
            body="# Lumio Overview\n\nLumio uses deterministic retrieval.",
            body_start_line=10,
        ),
        CompiledPage(
            path="stack.md",
            title="Technology Stack",
            id="entity:technology-stack",
            entity_types=["concept"],
            tags=["technology"],
            summary="The Python technology choices.",
            lifecycle="approved",
            visibility="public",
            sources=[Source(id="stack", title="Stack source")],
            body="# Technology Stack\n\nPython and msgspec power the foundation.",
            body_start_line=10,
        ),
    ]


def test_search_and_zero_index_retrieval_are_model_free(tmp_path: Path):
    pages = _pages()
    assert [item.page.title for item in search_pages(pages, "technology")] == ["Technology Stack"]
    results = ZeroIndexRetrieval().retrieve(pages, "deterministic retrieval", limit=1)
    assert len(results) == 1
    assert isinstance(results[0], RetrievalResult)
    assert results[0].citation.page_title == "Lumio Overview"
    assert results[0].trace.stages

    with pytest.raises(EmbeddingError, match="adapter|semantic|LanceDB"):
        ZeroIndexRetrieval().retrieve(pages, "Lumio", mode="semantic")


def _lumio_core_compat_available() -> bool:
    """ADR-0025: the temporary ``lumio.core`` compatibility surface lives in
    the private application repository; skip these alias checks when it (and
    therefore the application) is not installed."""
    return importlib.util.find_spec("lumio") is not None

@pytest.mark.skipif(
    not _lumio_core_compat_available(),
    reason="lumio.core lives in the private app repo (ADR-0025)",
)
def test_legacy_primitive_modules_alias_the_new_owner():
    for name in (
        "records",
        "embeddings",
        "evidence",
        "fingerprint_store",
        "page_search",
        "retrieval",
    ):
        assert importlib.import_module(f"lumio.core.{name}") is importlib.import_module(
            f"lumio_wiki.{name}"
        )
