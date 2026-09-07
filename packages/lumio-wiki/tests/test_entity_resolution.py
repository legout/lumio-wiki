"""Claim-aware retrieval metadata and entity-resolution candidates (issue #172).

Covers the deterministic entity-resolution surface and the retrieval
read-only invariants:

* ``resolve_entity`` returns ONE stable Entity from an exact Entity ID,
  exact Canonical Page Title, or exact alias — or a truthful ambiguity with
  every candidate. It never guesses.
* The ``entity`` CLI command exposes the same deterministic resolution plus
  optional LanceDB FTS/vector review candidates; without the enhanced
  adapter it is unavailable TRUTHFULLY (never silently empty).
* Entity-resolution candidates are review-only: no retrieval path authors,
  merges, accepts, disputes, or supersedes a Claim (issue #172 AC5).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lumio_wiki.cli import main
from lumio_wiki.knowledge_base import KnowledgeBase, load_knowledge_base
from lumio_wiki.records import EntityResolution

FIXTURES = Path(__file__).parent / "fixtures"

_CONTROL_V2 = """\
version: 2
mode: "categorized"
categories:
  - name: concepts
ontology:
  entity_types:
    concept: {}
    library: {}
  predicates:
    uses:
      subject_types: [concept]
      object_types: [concept, library]
"""


def _page(
    title: str,
    *,
    entity_id: str | None = None,
    aliases: tuple[str, ...] = (),
    body: str | None = None,
) -> str:
    slug = title.lower().replace(" ", "-")
    eid = entity_id or f"entity:{slug}"
    alias_list = ", ".join(f'"{alias}"' for alias in aliases)
    return (
        "---\n"
        f'id: "{eid}"\n'
        f'title: "{title}"\n'
        "entity_types:\n"
        "  - concept\n"
        f"aliases: [{alias_list}]\n"
        'tags:\n  - "test"\n'
        f'summary: "{title} summary."\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        "sources:\n"
        f'  - id: "src-{slug}"\n'
        f'    title: "{title} Source"\n'
        "---\n\n"
        f"# {title}\n\n{body or f'{title} body text.'}\n"
    )


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    root.mkdir(parents=True)
    (root / "lumio.yaml").write_text(_CONTROL_V2, encoding="utf-8")
    (root / "overview.md").write_text(
        _page("Lumio Overview", aliases=("Lumio", "Overview")), encoding="utf-8"
    )
    (root / "architecture.md").write_text(
        _page("Architecture", aliases=("Arch",)), encoding="utf-8"
    )
    # A second page sharing the "Lumio" alias makes alias resolution ambiguous.
    (root / "platform.md").write_text(
        _page("Lumio Platform", aliases=("Lumio",)), encoding="utf-8"
    )
    return root


@pytest.fixture
def kb(kb_root: Path) -> KnowledgeBase:
    kb, _report = load_knowledge_base(kb_root)
    return kb


# ---------------------------------------------------------------------------
# Deterministic exact/alias lookup (issue #172 AC3).
# ---------------------------------------------------------------------------


def test_resolve_entity_by_exact_id_returns_one_stable_entity(kb: KnowledgeBase):
    resolution = kb.resolve_entity("entity:lumio-overview")
    assert isinstance(resolution, EntityResolution)
    assert resolution.entity is not None
    assert resolution.entity.id == "entity:lumio-overview"
    assert resolution.entity.title == "Lumio Overview"
    assert resolution.matched_by == "entity-id"
    assert resolution.candidates == []


def test_resolve_entity_by_exact_canonical_title(kb: KnowledgeBase):
    resolution = kb.resolve_entity("Architecture")
    assert resolution.entity is not None
    assert resolution.entity.id == "entity:architecture"
    assert resolution.matched_by == "canonical-title"


def test_resolve_entity_by_exact_alias(kb: KnowledgeBase):
    resolution = kb.resolve_entity("Arch")
    assert resolution.entity is not None
    assert resolution.entity.id == "entity:architecture"
    assert resolution.matched_by == "alias"


def test_resolve_entity_ambiguous_alias_is_truthful(kb: KnowledgeBase):
    resolution = kb.resolve_entity("Lumio")
    assert resolution.entity is None
    assert resolution.matched_by == "alias"
    assert {candidate.id for candidate in resolution.candidates} == {
        "entity:lumio-overview",
        "entity:lumio-platform",
    }


def test_resolve_entity_unknown_name_is_unresolved_not_guessed(kb: KnowledgeBase):
    resolution = kb.resolve_entity("No Such Entity")
    assert resolution.entity is None
    assert resolution.candidates == []
    assert resolution.matched_by == ""


def test_resolve_entity_empty_name_is_unresolved(kb: KnowledgeBase):
    assert kb.resolve_entity("   ").entity is None


def test_resolve_entity_priority_id_then_title_then_alias(tmp_path: Path):
    # "entity:architecture" as an ALIAS of another page must not outrank the
    # exact Entity ID match on Architecture itself.
    root = tmp_path / "kb"
    root.mkdir(parents=True)
    (root / "lumio.yaml").write_text(_CONTROL_V2, encoding="utf-8")
    (root / "architecture.md").write_text(_page("Architecture"), encoding="utf-8")
    (root / "other.md").write_text(
        _page("Other Page", entity_id="entity:other", aliases=("entity:architecture",)),
        encoding="utf-8",
    )
    kb, _report = load_knowledge_base(root)
    resolution = kb.resolve_entity("entity:architecture")
    assert resolution.matched_by == "entity-id"
    assert resolution.entity is not None
    assert resolution.entity.title == "Architecture"


# ---------------------------------------------------------------------------
# Retrieval never mutates the Knowledge Base (issue #172 AC5).
# ---------------------------------------------------------------------------


def test_graph_retrieval_never_mutates_kb_files(kb_root: Path, kb: KnowledgeBase):
    before = {
        path.name: path.read_bytes()
        for path in sorted(kb_root.rglob("*"))
        if path.is_file()
    }
    kb.retrieve("Lumio body text", graph_seed_titles=["Lumio Overview"])
    kb.resolve_entity("Architecture")
    after = {
        path.name: path.read_bytes()
        for path in sorted(kb_root.rglob("*"))
        if path.is_file()
    }
    assert before == after


# ---------------------------------------------------------------------------
# CLI: ``lumio-wiki entity`` — deterministic resolution, always available.
# ---------------------------------------------------------------------------


def test_entity_command_resolves_by_alias(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(["entity", str(kb_root), "Arch"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Architecture" in out
    assert "entity:architecture" in out
    assert "matched by:  alias" in out


def test_entity_command_ambiguity_lists_candidates(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    rc = main(["entity", str(kb_root), "Lumio"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "ambiguous" in out.lower()
    assert "entity:lumio-overview" in out
    assert "entity:lumio-platform" in out


def test_entity_command_unresolved_returns_1(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    rc = main(["entity", str(kb_root), "No Such Entity"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No entity found" in err


def test_entity_command_candidates_need_lancedb(
    kb_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """AC4: FTS/vector resolution is unavailable TRUTHFULLY without the adapter."""
    import lumio_wiki.retrieval_eval as retrieval_eval

    monkeypatch.setattr(retrieval_eval, "lancedb_available", lambda: False)
    rc = main(["entity", str(kb_root), "Lumio", "--candidates"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "lumio-lancedb" in captured.err


# ---------------------------------------------------------------------------
# Legacy Flat Mode: no entity contract -> resolution is truthful, not fatal.
# ---------------------------------------------------------------------------


def test_resolve_entity_in_legacy_flat_kb_is_unresolved(tmp_path: Path):
    root = tmp_path / "flat"
    root.mkdir(parents=True)
    (root / "page.md").write_text(
        "---\ntitle: Legacy Page\n---\n\nNo entity contract.\n", encoding="utf-8"
    )
    kb, _report = load_knowledge_base(root)
    resolution = kb.resolve_entity("Legacy Page")
    assert resolution.entity is None
    assert resolution.candidates == []


# ---------------------------------------------------------------------------
# Review fixes: redirect resolution (ADR-0021) and the public SDK ID seam.
# ---------------------------------------------------------------------------


def test_resolve_entity_follows_ontology_redirect(tmp_path: Path):
    """A retired Entity ID resolves to its surviving Entity (ADR-0021)."""
    root = tmp_path / "kb"
    root.mkdir(parents=True)
    control = _CONTROL_V2 + "  redirects:\n    entity:old-arch: entity:architecture\n"
    (root / "lumio.yaml").write_text(control, encoding="utf-8")
    (root / "architecture.md").write_text(_page("Architecture"), encoding="utf-8")
    kb, _report = load_knowledge_base(root)
    resolution = kb.resolve_entity("entity:old-arch")
    assert resolution.entity is not None
    assert resolution.entity.id == "entity:architecture"
    assert resolution.matched_by == "redirect"


def test_entity_id_for_title_unknown_returns_none(kb: KnowledgeBase):
    assert kb.entity_id_for_title("No Such Page") is None


def test_entity_id_for_title_legacy_flat_returns_none(tmp_path: Path):
    root = tmp_path / "flat"
    root.mkdir(parents=True)
    (root / "page.md").write_text(
        "---\ntitle: Legacy Page\n---\n\nNo entity contract.\n", encoding="utf-8"
    )
    kb, _report = load_knowledge_base(root)
    assert kb.entity_id_for_title("Legacy Page") is None
