"""Tests for the opt-in semantic Dream Cycle maintenance review."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import lumio_wiki as lw
import pytest
from lumio_wiki.ingest import IngestStore
from lumio_wiki.semantic_maintenance import (
    MissingSemanticExtraError,
    SemanticDreamReviewer,
)

ROOT = Path(__file__).parents[3]
FIXTURE = ROOT / "tests" / "fixtures" / "categorized_kb"


def _response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
    )


def _client(payload: dict) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = _response(payload)
    return client


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    shutil.copytree(FIXTURE, root)
    return root


def _store(root: Path) -> IngestStore:
    directory = root / ".lumio" / "ingest"
    directory.mkdir(parents=True, exist_ok=True)
    return IngestStore(directory)


def test_reviewer_parses_findings_in_one_bounded_batch(monkeypatch, kb_root: Path):
    client = _client(
        {
            "findings": [
                {
                    "kind": "contradiction",
                    "pages": ["concepts/overview.md", "entities/acme.md"],
                    "reason": "The claims disagree.",
                    "note": "Review these claims together.",
                },
                {
                    "kind": "stale",
                    "page": "entities/acme.md",
                    "reason": "The page is superseded.",
                    "lifecycle": "deprecated",
                },
                {
                    "kind": "summary",
                    "page": "concepts/overview.md",
                    "reason": "The summary omits the main purpose.",
                    "summary": "Lumio is a deployable trusted-knowledge chat platform.",
                },
            ]
        }
    )
    monkeypatch.delenv("LUMIO_PROVIDER_MODEL", raising=False)
    kb, _ = lw.load_knowledge_base(kb_root)
    report = SemanticDreamReviewer(kb, client=client, max_pages=2).review()

    assert len(report.pages) == 2
    assert [finding.kind for finding in report.findings] == [
        "contradiction",
        "stale",
        "summary",
    ]
    assert client.chat.completions.create.call_count == 1
    request = client.chat.completions.create.call_args.kwargs
    assert request["model"] == "semantic-dream-review"
    assert "concepts/overview.md" in request["messages"][1]["content"]


def test_page_selection_is_bounded_and_hub_first(kb_root: Path):
    kb, _ = lw.load_knowledge_base(kb_root)
    # Add an authored Relationship so the overview is a deterministic hub.
    overview = kb_root / "concepts" / "overview.md"
    overview.write_text(
        overview.read_text(encoding="utf-8").replace(
            'type: "concept"',
            'type: "concept"\nrelationships:\n  - target: "Acme Corp"\n    type: "references"',
        ),
        encoding="utf-8",
    )
    kb, _ = lw.load_knowledge_base(kb_root)
    reviewer = SemanticDreamReviewer(kb, client=_client({"findings": []}), max_pages=1)

    selected = reviewer.select_pages()

    assert len(selected) == 1
    assert selected[0].path == "concepts/overview.md"


def test_staging_produces_only_unblocked_reviewable_proposals(kb_root: Path):
    client = _client(
        {
            "findings": [
                {
                    "kind": "stale",
                    "page": "entities/acme.md",
                    "reason": "Superseded by a newer record.",
                    "lifecycle": "deprecated",
                },
                {
                    "kind": "summary",
                    "page": "concepts/overview.md",
                    "reason": "Rewrite for clarity.",
                    "summary": "Lumio is trusted knowledge chat.",
                },
            ]
        }
    )
    kb, _ = lw.load_knowledge_base(kb_root)
    reviewer = SemanticDreamReviewer(kb, client=client)
    result = reviewer.stage_findings(store=_store(kb_root))

    assert len(result.staged) == 2
    assert not result.skipped
    assert all(proposal.status == "staged" and not proposal.blocked for proposal in result.staged)
    assert (
        not (kb_root / "concepts" / "overview.md")
        .read_text(encoding="utf-8")
        .endswith("Lumio is trusted knowledge chat.\n")
    )


def test_missing_extra_error_is_actionable(monkeypatch):
    import importlib

    real_import = importlib.import_module

    def block_openai(name, *args, **kwargs):
        if name == "openai":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", block_openai)
    with pytest.raises(MissingSemanticExtraError, match=r"pip install 'lumio-wiki\[llm\]'"):
        SemanticDreamReviewer(object(), model="fake-model")


def test_cli_semantic_review_reports_and_stages(monkeypatch, kb_root: Path, capsys):
    payload = {
        "findings": [
            {
                "kind": "summary",
                "page": "concepts/overview.md",
                "reason": "Make the hub summary more precise.",
                "summary": "Lumio is trusted knowledge chat.",
            }
        ]
    }
    client = _client(payload)
    monkeypatch.setenv("LUMIO_PROVIDER_MODEL", "fake-model")
    monkeypatch.setattr(SemanticDreamReviewer, "_build_client", staticmethod(lambda: client))

    from lumio_wiki.cli import main

    rc = main(["dream", str(kb_root), "--semantic", "--stage"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "# Semantic Dream Review" in out
    assert "semantic_summary: 1" in out
    assert "semantic_staged_proposals: 1" in out
    assert "Lumio is trusted knowledge chat." not in (
        kb_root / "concepts" / "overview.md"
    ).read_text(encoding="utf-8")


def test_contradiction_stages_body_callouts_on_both_pages(kb_root: Path):
    kb, _ = lw.load_knowledge_base(kb_root)
    reviewer = SemanticDreamReviewer(
        kb,
        client=_client(
            {
                "findings": [
                    {
                        "kind": "contradiction",
                        "pages": ["concepts/overview.md", "entities/acme.md"],
                        "reason": "The two statements disagree.",
                    }
                ]
            }
        ),
    )
    result = reviewer.stage_findings(store=_store(kb_root))
    assert len(result.staged) == 2
    for proposal in result.staged:
        assert "Semantic Dream Cycle contradiction" in proposal.proposed_pages[0].markdown


def test_invalid_findings_are_skipped_with_reasons_and_never_staged(kb_root: Path):
    kb, _ = lw.load_knowledge_base(kb_root)
    reviewer = SemanticDreamReviewer(
        kb,
        client=_client(
            {
                "findings": [
                    {"kind": "mystery", "page": "concepts/overview.md", "reason": "bad kind"},
                    {
                        "kind": "summary",
                        "page": "not-inspected.md",
                        "reason": "bad page",
                        "summary": "new",
                    },
                    {
                        "kind": "stale",
                        "page": "concepts/overview.md",
                        "reason": "bad lifecycle",
                        "lifecycle": "approved",
                    },
                    {
                        "kind": "summary",
                        "page": "concepts/overview.md",
                        "reason": "extra key",
                        "summary": "new",
                        "frontmatter": {"owner": "x"},
                    },
                ]
            }
        ),
        max_pages=1,
    )
    result = reviewer.stage_findings(store=_store(kb_root))
    assert not result.staged
    reasons = " ".join(reason for _, reason in result.skipped)
    assert "unknown finding kind" in reasons
    assert "not in the inspected page set" in reasons
    assert "illegal stale lifecycle" in reasons
    assert "unsupported finding fields" in reasons


def test_blocked_semantic_candidate_is_skipped(kb_root: Path, monkeypatch):
    kb, _ = lw.load_knowledge_base(kb_root)
    reviewer = SemanticDreamReviewer(
        kb,
        client=_client(
            {
                "findings": [
                    {
                        "kind": "summary",
                        "page": "concepts/overview.md",
                        "reason": "rewrite",
                        "summary": "A new summary",
                    }
                ]
            }
        ),
    )
    monkeypatch.setattr(reviewer, "_assemble_unblocked", lambda *args: None)
    result = reviewer.stage_findings(store=_store(kb_root))
    assert not result.staged
    assert "blocked" in result.skipped[0][1]
