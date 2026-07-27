"""Opt-in LLM-assisted maintenance for the semantic Dream Cycle.

The deterministic Dream Cycle remains the first reflection pass.  This module
adds a bounded, read-only semantic review and routes every optional mutation
through the existing proposal pipeline.  The OpenAI SDK is imported lazily so
that the base ``lumio-wiki`` installation remains model-free.
"""

from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgspec

from lumio_wiki.ingest import IngestProposal, IngestStore, SourceProvenance
from lumio_wiki.knowledge_base import GRAPH_SCOPE_DISCOVERY, KnowledgeBase
from lumio_wiki.maintenance import _revision_routing_fields, mark_compound_revision
from lumio_wiki.proposal_pipeline import ProposalPipeline
from lumio_wiki.records import CompiledPage

_SEMANTIC_KINDS = frozenset(("contradiction", "stale", "summary"))
_KIND_ORDER = {"contradiction": 0, "stale": 1, "summary": 2}
_LLM_EXTRA_HINT = "pip install 'lumio-wiki[llm]'"


class SemanticMaintenanceError(RuntimeError):
    """Raised when semantic maintenance cannot complete actionably."""


class MissingSemanticExtraError(SemanticMaintenanceError):
    """Raised when the optional OpenAI-compatible provider is unavailable."""


@dataclass(frozen=True, slots=True)
class SemanticFinding:
    """One normalized semantic finding returned by the provider."""

    kind: str
    pages: tuple[str, ...]
    reason: str
    note: str | None = None
    lifecycle: str | None = None
    summary: str | None = None

    @property
    def page(self) -> str | None:
        """Return the single target page for page-scoped findings."""
        return self.pages[0] if self.pages else None


@dataclass(frozen=True, slots=True)
class SemanticReviewReport:
    """Read-only semantic review output and the bounded pages it inspected."""

    pages: tuple[CompiledPage, ...]
    findings: tuple[SemanticFinding, ...]


@dataclass(frozen=True, slots=True)
class SemanticStagingResult:
    """Reviewable proposals created from semantic findings and skipped findings."""

    staged: tuple[IngestProposal, ...] = ()
    skipped: tuple[tuple[SemanticFinding, str], ...] = ()


class SemanticDreamReviewer:
    """Review a bounded Knowledge Base page batch with one provider call.

    ``client`` is an OpenAI-compatible client exposing
    ``client.chat.completions.create``.  Injecting it is the supported test and
    custom-provider seam.  When omitted, the client is built from
    ``LUMIO_PROVIDER_BASE_URL``, ``LUMIO_PROVIDER_API_KEY``, and
    ``LUMIO_PROVIDER_MODEL``.
    """

    def __init__(
        self,
        kb: KnowledgeBase,
        client: Any | None = None,
        *,
        model: str | None = None,
        max_pages: int = 25,
    ) -> None:
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        self.kb = kb
        self.max_pages = max_pages
        configured_model = model or os.environ.get("LUMIO_PROVIDER_MODEL")
        if client is None:
            # Build first so a base install always reports the missing extra,
            # even when provider configuration is also absent.
            self._client = self._build_client()
            if not configured_model:
                raise SemanticMaintenanceError(
                    "semantic Dream Cycle requires LUMIO_PROVIDER_MODEL "
                    "(set LUMIO_PROVIDER_BASE_URL / LUMIO_PROVIDER_API_KEY for an "
                    "OpenAI-compatible endpoint)."
                )
        else:
            self._client = client
        self.model = configured_model or "semantic-dream-review"

    @staticmethod
    def _build_client() -> Any:
        try:
            openai_module = importlib.import_module("openai")
        except ImportError as exc:
            raise MissingSemanticExtraError(
                "cannot run semantic Dream Cycle: the lumio-wiki[llm] extra is "
                f"required; install it with:  {_LLM_EXTRA_HINT}"
            ) from exc
        kwargs: dict[str, str] = {}
        base_url = os.environ.get("LUMIO_PROVIDER_BASE_URL")
        api_key = os.environ.get("LUMIO_PROVIDER_API_KEY")
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        try:
            return openai_module.OpenAI(**kwargs)
        except Exception as exc:  # noqa: BLE001 - SDK/provider surface is open
            raise SemanticMaintenanceError(
                "could not configure the semantic review provider; set "
                "LUMIO_PROVIDER_API_KEY and optionally LUMIO_PROVIDER_BASE_URL "
                f"for an OpenAI-compatible endpoint ({exc})"
            ) from exc

    def select_pages(self) -> tuple[CompiledPage, ...]:
        """Select hubs first, then the remaining pages in stable path order."""
        diagnostics = self.kb.graph_diagnostics(
            scope=GRAPH_SCOPE_DISCOVERY,
            max_hub_sample=self.max_pages,
        )
        hub_scores: dict[str, int] = {}
        for hub in (*diagnostics.top_inbound_hubs, *diagnostics.top_outbound_hubs):
            hub_scores[hub.title] = max(hub_scores.get(hub.title, 0), hub.edge_count)

        selected = sorted(
            self.kb.pages,
            key=lambda page: (-hub_scores.get(page.title, 0), page.path, page.title),
        )
        return tuple(selected[: self.max_pages])

    def review(self) -> SemanticReviewReport:
        """Run exactly one bounded provider review and normalize its findings."""
        pages = self.select_pages()
        if not pages:
            return SemanticReviewReport(pages=(), findings=())
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a Lumio Knowledge Base Maintainer. Review the supplied "
                    "Compiled Pages for only three semantic maintenance signals: "
                    "contradiction, stale, and summary. Return ONLY a JSON object "
                    "with a 'findings' array. Each finding must use kind, pages (paths), "
                    "reason, and for stale lifecycle (review or deprecated), for summary "
                    "summary, or for contradiction note. Do not invent facts."
                ),
            },
            {"role": "user", "content": self._review_payload(pages)},
        ]
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001 - provider surface is open
            raise SemanticMaintenanceError(
                f"semantic review provider request failed: {exc}"
            ) from exc
        content = self._response_content(response)
        return SemanticReviewReport(pages=pages, findings=self.parse_findings(content, pages))

    @staticmethod
    def _review_payload(pages: tuple[CompiledPage, ...]) -> str:
        return json.dumps(
            {
                "pages": [
                    {
                        "path": page.path,
                        "title": page.title,
                        "summary": page.summary,
                        "lifecycle": page.lifecycle,
                        "body": page.body,
                    }
                    for page in pages
                ]
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _response_content(response: Any) -> str:
        choices = getattr(response, "choices", None)
        if not choices:
            raise SemanticMaintenanceError("semantic review provider returned no completion choice")
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if content is None or not str(content).strip():
            raise SemanticMaintenanceError("semantic review provider returned empty content")
        return str(content)

    @classmethod
    def parse_findings(
        cls,
        content: str,
        pages: tuple[CompiledPage, ...] | list[CompiledPage],
    ) -> tuple[SemanticFinding, ...]:
        """Parse and validate provider JSON against the inspected page set."""
        raw = content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            raw = raw.rsplit("```", 1)[0].strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SemanticMaintenanceError(f"semantic review returned invalid JSON: {exc}") from exc
        if isinstance(payload, list):
            raw_findings = payload
        elif isinstance(payload, dict):
            raw_findings = payload.get("findings", [])
        else:
            raw_findings = []
        if not isinstance(raw_findings, list):
            raise SemanticMaintenanceError("semantic review JSON 'findings' must be an array")

        by_path = {page.path: page.path for page in pages}
        by_title = {page.title: page.path for page in pages}
        findings: list[SemanticFinding] = []
        for raw_finding in raw_findings:
            if not isinstance(raw_finding, dict):
                continue
            kind = str(raw_finding.get("kind", "")).strip().lower()
            if kind not in _SEMANTIC_KINDS:
                continue
            references = raw_finding.get("pages")
            if references is None:
                references = raw_finding.get("page_paths")
            if references is None:
                references = raw_finding.get("involved_pages")
            if references is None:
                references = [raw_finding.get("page")]
            if isinstance(references, str):
                references = [references]
            if not isinstance(references, list):
                continue
            normalized_pages: list[str] = []
            for reference in references:
                if not isinstance(reference, str):
                    continue
                resolved = by_path.get(reference) or by_title.get(reference)
                if resolved and resolved not in normalized_pages:
                    normalized_pages.append(resolved)
            if not normalized_pages:
                continue
            if kind == "contradiction" and len(normalized_pages) < 2:
                continue
            if kind in {"stale", "summary"}:
                normalized_pages = normalized_pages[:1]
            reason = str(raw_finding.get("reason", "")).strip()
            if not reason:
                continue
            lifecycle = raw_finding.get(
                "lifecycle",
                raw_finding.get("new_lifecycle", raw_finding.get("suggested_lifecycle")),
            )
            lifecycle_value = str(lifecycle).strip().lower() if lifecycle is not None else None
            if lifecycle_value not in {"review", "deprecated"}:
                lifecycle_value = None
            summary = raw_finding.get(
                "summary",
                raw_finding.get("replacement_summary", raw_finding.get("suggested_summary")),
            )
            summary_value = str(summary).strip() if summary is not None else None
            if summary_value == "":
                summary_value = None
            note = raw_finding.get("note", raw_finding.get("callout"))
            note_value = str(note).strip() if note is not None else None
            findings.append(
                SemanticFinding(
                    kind=kind,
                    pages=tuple(normalized_pages),
                    reason=reason,
                    note=note_value or None,
                    lifecycle=lifecycle_value,
                    summary=summary_value,
                )
            )
        return tuple(
            sorted(
                findings,
                key=lambda finding: (
                    _KIND_ORDER[finding.kind],
                    finding.pages,
                    finding.reason,
                    finding.summary or "",
                ),
            )
        )

    def stage_findings(
        self,
        *,
        store: IngestStore,
        report: SemanticReviewReport | None = None,
    ) -> SemanticStagingResult:
        """Stage valid finding edits, skipping blocked candidates with reasons."""
        report = report or self.review()
        staged: list[IngestProposal] = []
        skipped: list[tuple[SemanticFinding, str]] = []
        for finding in report.findings:
            try:
                edits = self._edits_for_finding(finding)
            except (OSError, SemanticMaintenanceError, ValueError) as exc:
                skipped.append((finding, str(exc)))
                continue
            if not edits:
                skipped.append((finding, "finding has no actionable edit"))
                continue
            for page_path, edited in edits:
                try:
                    proposal = self._assemble_unblocked(store, page_path, edited)
                except (OSError, SemanticMaintenanceError, ValueError) as exc:
                    skipped.append((finding, str(exc)))
                    continue
                if proposal is None:
                    skipped.append((finding, "candidate validation blocked the proposal"))
                    continue
                staged.append(proposal)
        return SemanticStagingResult(staged=tuple(staged), skipped=tuple(skipped))

    def _edits_for_finding(self, finding: SemanticFinding) -> list[tuple[str, str]]:
        edits: list[tuple[str, str]] = []
        for page_path in finding.pages:
            source = Path(self.kb.root) / page_path
            markdown = source.read_text(encoding="utf-8")
            if finding.kind == "stale":
                lifecycle = finding.lifecycle or "deprecated"
                page = next((page for page in self.kb.pages if page.path == page_path), None)
                if page is None or page.lifecycle == lifecycle:
                    continue
                if lifecycle == "review" and page.lifecycle != "approved":
                    continue
                if lifecycle == "deprecated" and page.lifecycle not in {
                    "draft",
                    "review",
                    "approved",
                }:
                    continue
                edited = _update_frontmatter(markdown, {"lifecycle": lifecycle})
            elif finding.kind == "summary":
                if not finding.summary:
                    continue
                edited = _update_frontmatter(markdown, {"summary": finding.summary})
            else:
                note = finding.note or finding.reason
                note_lines = "\n".join(f"> {line}" for line in note.splitlines())
                callout = (
                    "\n\n> [!warning] Semantic Dream Cycle contradiction\n"
                    f"> Review with: {', '.join(finding.pages)}\n"
                    f"> Reason: {finding.reason}\n{note_lines}\n"
                )
                edited = markdown.rstrip("\n") + callout
            edited = mark_compound_revision(
                edited,
                **_revision_routing_fields(self.kb, page_path),
            )
            edits.append((page_path, edited))
        return edits

    def _assemble_unblocked(
        self,
        store: IngestStore,
        page_path: str,
        edited: str,
    ) -> IngestProposal | None:
        provenance = SourceProvenance(
            original_filename=page_path,
            content_type="text/markdown",
            converted_by="semantic-dream",
            origin="semantic-dream",
        )
        pipeline = ProposalPipeline(self.kb, store=store)
        proposal = pipeline.assemble(edited, provenance, page_path)
        if proposal.blocked:
            return None
        return pipeline.stage(proposal)



def _update_frontmatter(markdown: str, updates: dict[str, Any]) -> str:
    if not markdown.startswith("---"):
        raise SemanticMaintenanceError("page has no YAML frontmatter")
    _leading, yaml_text, body = markdown.split("---", 2)
    data = msgspec.yaml.decode(yaml_text)
    if not isinstance(data, dict):
        raise SemanticMaintenanceError("page frontmatter did not decode to a mapping")
    data.update(updates)
    encoded = msgspec.yaml.encode(data).decode("utf-8")
    return f"---\n{encoded}---{body}"


__all__ = [
    "MissingSemanticExtraError",
    "SemanticDreamReviewer",
    "SemanticFinding",
    "SemanticMaintenanceError",
    "SemanticReviewReport",
    "SemanticStagingResult",
]
