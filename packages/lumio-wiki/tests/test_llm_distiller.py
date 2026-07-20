"""Issue #101: the ``lumio-wiki[llm]`` unattended OpenAI-compatible Distiller.

These tests lock in the runtime behavior the ``[llm]`` extra adds on top of the
model-free base ``lumio-wiki`` (AC1–AC5). They NEVER make a real network call:
every provider call is satisfied by an injectable fake OpenAI-compatible chat
client. The isolated-wheel proof (AC6) lives in ``test_wheel_isolation_llm.py``.

Coverage:
- ``OpenAIDistiller`` implements the shared ``Distiller`` Protocol with the same
  input/output shape as ``PassthroughMarkdownDistiller`` (AC2, AC3).
- ``openai`` is imported lazily and the missing-extra path raises an actionable
  error naming the exact install command (AC4, PRD user story 18).
- Structured-output retry/error behavior: transient provider errors are retried
  and an empty/malformed model response surfaces an actionable error (AC2).
- Provider-driven proposals flow through the same ``ProposalPipeline.assemble``
  so provenance, category/type routing, durability rationale, validation, diff,
  and blast radius are identical to the model-free path (AC3).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import lumio_wiki as lw
import pytest
from lumio_wiki.distiller import (
    Distiller,
    OpenAIDistiller,
    OpenAIDistillerError,
)
from lumio_wiki.proposal_pipeline import ProposalPipeline
from lumio_wiki.source_processor import TextMarkdownSourceProcessor

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "valid"

# A minimal valid Compiled Page Markdown the provider would return. It carries
# the routing/durability fields a distilled page is expected to declare.
PROVIDER_MARKDOWN = """\
---
title: "Distilled Page"
aliases: []
tags:
  - "llm"
summary: "Authored by the unattended OpenAI-compatible Distiller."
category: "concepts"
type: "definition"
durability_rationale: "Core concept referenced across the source."
lifecycle: "draft"
visibility: "internal"
sources:
  - id: "llm"
    title: "Distilled source"
relationships: []
synthetic: false
---

# Distilled Page

Body distilled by the provider.
"""


def _chat_completion(content: str | None) -> SimpleNamespace:
    """Build a minimal OpenAI chat-completion response object."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _fake_client(content: str | None = PROVIDER_MARKDOWN) -> MagicMock:
    """A fake OpenAI-compatible client whose ``chat.completions.create`` returns ``content``."""
    client = MagicMock()
    client.chat.completions.create.return_value = _chat_completion(content)
    return client


class _TransientError(Exception):
    """A transient provider error that should be retried."""


# ---------------------------------------------------------------------------
# Interface conformance (AC2)
# ---------------------------------------------------------------------------


def test_openai_distiller_satisfies_the_shared_distiller_protocol():
    """AC2: the unattended Distiller implements the shared Distiller interface."""
    distiller = OpenAIDistiller(model="fake-model", client=_fake_client())
    assert isinstance(distiller, Distiller)


def test_openai_distiller_distill_returns_the_provider_markdown():
    """AC2: ``distill`` returns the provider's Markdown verbatim (same shape as Passthrough)."""
    distiller = OpenAIDistiller(model="fake-model", client=_fake_client("raw markdown"))
    normalized = TextMarkdownSourceProcessor().process(
        "source.txt", "text/plain", b"some source text"
    )
    assert distiller.distill(normalized) == "raw markdown"


# ---------------------------------------------------------------------------
# Lazy import + actionable missing-extra error (AC4)
# ---------------------------------------------------------------------------


def test_openai_distiller_raises_actionable_error_when_openai_missing(monkeypatch):
    """AC4 + PRD user story 18: the missing-extra error names the exact extra.

    The base install does not include ``openai``. Constructing an
    ``OpenAIDistiller`` without an injected client must therefore raise an
    error that tells the user exactly what to install.
    """
    # Simulate the base install where ``openai`` is absent.
    import builtins

    real_import = builtins.__import__

    def _block_openai(name, *args, **kwargs):
        if name == "openai":
            raise ImportError("simulated missing extra")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_openai)
    with pytest.raises(OpenAIDistillerError) as exc_info:
        OpenAIDistiller(model="fake-model")  # no client injected
    message = str(exc_info.value)
    assert "pip install 'lumio-wiki[llm]'" in message, (
        "the missing-extra error must name the exact install command"
    )


def test_openai_distiller_with_injected_client_does_not_import_openai():
    """AC1: with a client injected, the Distiller never imports ``openai`` at runtime."""
    import sys

    sys.modules.pop("openai", None)
    distiller = OpenAIDistiller(model="fake-model", client=_fake_client())
    normalized = TextMarkdownSourceProcessor().process(
        "source.txt", "text/plain", b"text"
    )
    distiller.distill(normalized)
    # ``openai`` was never imported because the client was injected.
    assert "openai" not in sys.modules


# ---------------------------------------------------------------------------
# Structured-output retry/error behavior (AC2)
# ---------------------------------------------------------------------------


def test_openai_distiller_retries_transient_provider_errors():
    """AC2: transient provider errors are retried up to the configured limit."""
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _TransientError("transient 1"),
        _TransientError("transient 2"),
        _chat_completion(PROVIDER_MARKDOWN),
    ]
    distiller = OpenAIDistiller(
        model="fake-model",
        client=client,
        max_retries=3,
        backoff_seconds=0,
        retry_errors=("_TransientError",),
    )
    normalized = TextMarkdownSourceProcessor().process(
        "source.txt", "text/plain", b"text"
    )
    assert distiller.distill(normalized) == PROVIDER_MARKDOWN
    assert client.chat.completions.create.call_count == 3


def test_openai_distiller_raises_after_exhausting_retries():
    """AC2: when retries are exhausted, the actionable error surfaces the last failure."""
    client = MagicMock()
    client.chat.completions.create.side_effect = _TransientError("always fails")
    distiller = OpenAIDistiller(
        model="fake-model",
        client=client,
        max_retries=2,
        backoff_seconds=0,
        retry_errors=("_TransientError",),
    )
    normalized = TextMarkdownSourceProcessor().process(
        "source.txt", "text/plain", b"text"
    )
    with pytest.raises(OpenAIDistillerError, match="lumio-wiki\\[llm\\]|provider"):
        distiller.distill(normalized)
    # initial attempt + 2 retries == 3 total calls
    assert client.chat.completions.create.call_count == 3


def test_openai_distiller_surfaces_empty_model_output_as_actionable_error():
    """AC2: an empty / None model response surfaces an actionable error."""
    client = _fake_client(content=None)
    distiller = OpenAIDistiller(
        model="fake-model", client=client, max_retries=1, backoff_seconds=0
    )
    normalized = TextMarkdownSourceProcessor().process(
        "source.txt", "text/plain", b"text"
    )
    with pytest.raises(OpenAIDistillerError, match="empty|no content"):
        distiller.distill(normalized)


def test_openai_distiller_non_retryable_error_is_not_retried():
    """AC2: a non-transient (e.g. auth) error is not retried and surfaces immediately."""

    class _AuthError(Exception):
        pass

    client = MagicMock()
    client.chat.completions.create.side_effect = _AuthError("bad key")
    distiller = OpenAIDistiller(
        model="fake-model",
        client=client,
        max_retries=3,
        backoff_seconds=0,
        retry_errors=("_TransientError",),  # only retry this one
    )
    normalized = TextMarkdownSourceProcessor().process(
        "source.txt", "text/plain", b"text"
    )
    with pytest.raises(OpenAIDistillerError):
        distiller.distill(normalized)
    assert client.chat.completions.create.call_count == 1


# ---------------------------------------------------------------------------
# Category-aware prompt (AC3 — categories flow into the provider prompt)
# ---------------------------------------------------------------------------


def test_openai_distiller_passes_configured_categories_into_the_prompt():
    """AC3: the configured Content Category catalog is offered to the provider.

    A custom-catalog Knowledge Base must only ever see its own categories in
    the distill prompt — never a hard-coded catalog (issue #78, P2.7).
    """
    client = _fake_client()
    distiller = OpenAIDistiller(model="fake-model", client=client)
    normalized = TextMarkdownSourceProcessor().process(
        "source.txt", "text/plain", b"text"
    )
    distiller.distill(normalized, categories=["concepts", "synthesis"])
    args, kwargs = client.chat.completions.create.call_args
    messages = kwargs.get("messages") or args[0]
    system = next(m["content"] for m in messages if m["role"] == "system")
    assert "concepts" in system
    assert "synthesis" in system


# ---------------------------------------------------------------------------
# Provider-driven proposals preserve provenance / routing / validation / diff /
# blast radius (AC3)
# ---------------------------------------------------------------------------


def _kb(tmp_path: Path):
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES, root)
    kb, report = lw.load_knowledge_base(root)
    assert report.is_valid, report
    return kb


def test_provider_distilled_markdown_flows_through_the_proposal_pipeline(tmp_path: Path):
    """AC3: provider-driven distillation enters the same proposal-first pipeline."""
    kb = _kb(tmp_path)
    store = lw.IngestStore(tmp_path / "ingest")
    distiller = OpenAIDistiller(model="fake-model", client=_fake_client())
    normalized = TextMarkdownSourceProcessor().process(
        "source.txt", "text/plain", b"raw source"
    )
    provenance = lw.SourceProvenance(
        original_filename="source.txt",
        content_type="text/plain",
        converted_by=normalized.converted_by,
        source_hash=normalized.source_hash,
    )
    distilled = distiller.distill(normalized)
    pipeline = ProposalPipeline(kb, store=store)
    proposal = pipeline.assemble(distilled, provenance, "source.txt")
    proposal = pipeline.stage(proposal)

    # Same proposal shape as host-agent ingestion.
    assert isinstance(proposal, lw.IngestProposal)
    assert proposal.status == "staged"
    assert proposal.affected_pages == ["Distilled Page"]
    # Provenance preserved from the normalized source.
    assert proposal.provenance.original_filename == "source.txt"
    assert proposal.provenance.source_hash == normalized.source_hash
    # Category/type/durability routing preserved on the proposed page.
    page = proposal.proposed_pages[0]
    assert page.category == "concepts"
    assert page.page_type == "definition"
    assert page.durability_rationale.startswith("Core concept")
    # Validation, diff, and blast radius are computed identically.
    assert isinstance(proposal.validation_report, lw.ValidationReport)
    assert proposal.diff  # non-empty diff
    assert proposal.blast_radius is not None
    assert "Distilled Page" in proposal.blast_radius.new_titles


def test_provider_driven_and_passthrough_proposals_share_the_same_shape(tmp_path: Path):
    """AC3: provider-driven and host-agent proposals are structurally identical.

    Both paths feed the same ``ProposalPipeline.assemble`` so the resulting
    ``IngestProposal`` carries the same fields in the same order — the
    Distiller is the only seam that changes.
    """
    kb = _kb(tmp_path)
    # Both distillers produce the same Markdown; the only difference is who
    # authored it. The proposal shape downstream must be identical.
    passthrough = lw.PassthroughMarkdownDistiller()
    provenance = lw.SourceProvenance(
        original_filename="source.txt",
        content_type="text/plain",
        converted_by="markdown",
        source_hash="abc",
    )
    pipeline = ProposalPipeline(kb)
    a = pipeline.assemble(
        passthrough.distill(
            TextMarkdownSourceProcessor().process(
                "source.txt", "text/plain", PROVIDER_MARKDOWN.encode("utf-8")
            ),
            categories=["concepts"],
        ),
        provenance,
        "source.txt",
    )
    b = pipeline.assemble(PROVIDER_MARKDOWN, provenance, "source.txt")
    assert set(a.__struct_fields__) == set(b.__struct_fields__)
    assert [p.title for p in a.proposed_pages] == [p.title for p in b.proposed_pages]
