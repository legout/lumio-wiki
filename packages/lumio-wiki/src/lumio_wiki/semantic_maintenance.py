"""Opt-in LLM-assisted maintenance for the semantic Dream Cycle."""

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
_ALLOWED_FINDING_FIELDS = frozenset(
    {
        "kind",
        "pages",
        "page_paths",
        "involved_pages",
        "page",
        "reason",
        "note",
        "callout",
        "lifecycle",
        "new_lifecycle",
        "suggested_lifecycle",
        "summary",
        "replacement_summary",
        "suggested_summary",
    }
)
_CONTRADICTION_MARKER = "> [!warning] Semantic Dream Cycle contradiction"


class SemanticMaintenanceError(RuntimeError):
    """Raised when semantic maintenance cannot complete actionably."""


class MissingSemanticExtraError(SemanticMaintenanceError):
    """Raised when the optional OpenAI-compatible provider is unavailable."""


@dataclass(frozen=True, slots=True)
class SemanticFinding:
    """One provider finding, retained even when sanitization rejects it."""

    kind: str
    pages: tuple[str, ...]
    reason: str
    note: str | None = None
    lifecycle: str | None = None
    summary: str | None = None
    validation_errors: tuple[str, ...] = ()

    @property
    def page(self) -> str | None:
        return self.pages[0] if self.pages else None


@dataclass(frozen=True, slots=True)
class SemanticReviewReport:
    pages: tuple[CompiledPage, ...]
    findings: tuple[SemanticFinding, ...]


@dataclass(frozen=True, slots=True)
class SemanticStagingResult:
    staged: tuple[IngestProposal, ...] = ()
    skipped: tuple[tuple[SemanticFinding, str], ...] = ()


class SemanticDreamReviewer:
    """Review a bounded Knowledge Base page batch with one provider call."""

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
        if base_url := os.environ.get("LUMIO_PROVIDER_BASE_URL"):
            kwargs["base_url"] = base_url
        if api_key := os.environ.get("LUMIO_PROVIDER_API_KEY"):
            kwargs["api_key"] = api_key
        try:
            return openai_module.OpenAI(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise SemanticMaintenanceError(
                "could not configure the semantic review provider; set "
                f"LUMIO_PROVIDER_API_KEY and optionally LUMIO_PROVIDER_BASE_URL "
                f"for an OpenAI-compatible endpoint ({exc})"
            ) from exc

    def select_pages(self) -> tuple[CompiledPage, ...]:
        diagnostics = self.kb.graph_diagnostics(
            scope=GRAPH_SCOPE_DISCOVERY, max_hub_sample=self.max_pages
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
            response = self._client.chat.completions.create(model=self.model, messages=messages)
        except Exception as exc:  # noqa: BLE001
            raise SemanticMaintenanceError(
                f"semantic review provider request failed: {exc}"
            ) from exc
        return SemanticReviewReport(
            pages=pages, findings=self.parse_findings(self._response_content(response), pages)
        )

    @staticmethod
    def _review_payload(pages: tuple[CompiledPage, ...]) -> str:
        return json.dumps(
            {
                "pages": [
                    {
                        "path": p.path,
                        "title": p.title,
                        "summary": p.summary,
                        "lifecycle": p.lifecycle,
                        "body": p.body,
                    }
                    for p in pages
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
        cls, content: str, pages: tuple[CompiledPage, ...] | list[CompiledPage]
    ) -> tuple[SemanticFinding, ...]:
        raw = content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            raw = raw.rsplit("```", 1)[0].strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SemanticMaintenanceError(f"semantic review returned invalid JSON: {exc}") from exc
        raw_findings = (
            payload
            if isinstance(payload, list)
            else payload.get("findings", [])
            if isinstance(payload, dict)
            else []
        )
        if not isinstance(raw_findings, list):
            raise SemanticMaintenanceError("semantic review JSON 'findings' must be an array")
        by_path = {page.path: page.path for page in pages}
        by_title = {page.title: page.path for page in pages}
        findings: list[SemanticFinding] = []
        for raw_finding in raw_findings:
            if not isinstance(raw_finding, dict):
                continue
            errors: list[str] = []
            extra = sorted(set(raw_finding) - _ALLOWED_FINDING_FIELDS)
            if extra:
                errors.append(f"unsupported finding fields: {', '.join(extra)}")
            kind = str(raw_finding.get("kind", "")).strip().lower()
            if kind not in _SEMANTIC_KINDS:
                errors.append(f"unknown finding kind: {kind or '<missing>'}")
            references = raw_finding.get(
                "pages", raw_finding.get("page_paths", raw_finding.get("involved_pages"))
            )
            if references is None:
                references = [raw_finding.get("page")]
            if isinstance(references, str):
                references = [references]
            if not isinstance(references, list):
                references = []
                errors.append("pages must be an array or string")
            normalized_pages: list[str] = []
            for reference in references:
                if not isinstance(reference, str):
                    errors.append("page references must be strings")
                    continue
                normalized_pages.append(
                    by_path.get(reference) or by_title.get(reference) or reference
                )
            reason = str(raw_finding.get("reason", "")).strip()
            if not reason:
                errors.append("finding reason is required")
            if kind == "contradiction" and len(normalized_pages) < 2:
                errors.append("contradiction requires two inspected pages")
            lifecycle_raw = raw_finding.get(
                "lifecycle",
                raw_finding.get("new_lifecycle", raw_finding.get("suggested_lifecycle")),
            )
            lifecycle = str(lifecycle_raw).strip().lower() if lifecycle_raw is not None else None
            if kind == "stale" and lifecycle not in {"review", "deprecated"}:
                errors.append(f"illegal stale lifecycle: {lifecycle or '<missing>'}")
            summary_raw = raw_finding.get(
                "summary",
                raw_finding.get("replacement_summary", raw_finding.get("suggested_summary")),
            )
            summary = str(summary_raw).strip() if summary_raw is not None else None
            if kind == "summary" and not summary:
                errors.append("summary replacement is required")
            note_raw = raw_finding.get("note", raw_finding.get("callout"))
            note = str(note_raw).strip() if note_raw is not None else None
            findings.append(
                SemanticFinding(
                    kind,
                    tuple(dict.fromkeys(normalized_pages)),
                    reason,
                    note or None,
                    lifecycle,
                    summary or None,
                    tuple(errors),
                )
            )
        return tuple(
            sorted(
                findings,
                key=lambda f: (_KIND_ORDER.get(f.kind, 99), f.pages, f.reason, f.summary or ""),
            )
        )

    def stage_findings(
        self, *, store: IngestStore, report: SemanticReviewReport | None = None
    ) -> SemanticStagingResult:
        report = report or self.review()
        inspected = frozenset(page.path for page in report.pages)
        staged: list[IngestProposal] = []
        skipped: list[tuple[SemanticFinding, str]] = []
        for finding in report.findings:
            try:
                edits = self._edits_for_finding(finding, inspected)
            except (OSError, SemanticMaintenanceError, ValueError, msgspec.DecodeError) as exc:
                skipped.append((finding, str(exc)))
                continue
            if not edits:
                skipped.append((finding, "finding has no actionable edit"))
                continue
            for page_path, edited in edits:
                try:
                    proposal = self._assemble_unblocked(store, page_path, edited)
                except (OSError, SemanticMaintenanceError, ValueError, msgspec.DecodeError) as exc:
                    skipped.append((finding, str(exc)))
                    continue
                if proposal is None:
                    skipped.append((finding, "candidate validation blocked the proposal"))
                else:
                    staged.append(proposal)
        return SemanticStagingResult(tuple(staged), tuple(skipped))

    @staticmethod
    def _stale_target_lifecycle(finding: SemanticFinding) -> str:
        """Single source of truth used by both output and staging."""
        return finding.lifecycle or "deprecated"

    def _edits_for_finding(
        self, finding: SemanticFinding, inspected: frozenset[str]
    ) -> list[tuple[str, str]]:
        if finding.validation_errors:
            raise SemanticMaintenanceError("; ".join(finding.validation_errors))
        if finding.kind not in _SEMANTIC_KINDS:
            raise SemanticMaintenanceError(f"unknown finding kind: {finding.kind or '<missing>'}")
        if not finding.pages:
            raise SemanticMaintenanceError("finding has no page references")
        if any(path not in inspected for path in finding.pages):
            raise SemanticMaintenanceError("page is not in the inspected page set")
        if finding.kind == "contradiction" and len(finding.pages) < 2:
            raise SemanticMaintenanceError("contradiction requires two involved pages")
        if finding.kind in {"stale", "summary"} and len(finding.pages) != 1:
            raise SemanticMaintenanceError("page-scoped finding must target one page")
        edits: list[tuple[str, str]] = []
        for page_path in finding.pages:
            source = Path(self.kb.root) / page_path
            markdown = source.read_text(encoding="utf-8")
            page = next((p for p in self.kb.pages if p.path == page_path), None)
            if page is None:
                raise SemanticMaintenanceError(f"page does not exist: {page_path}")
            if finding.kind == "stale":
                lifecycle = self._stale_target_lifecycle(finding)
                if lifecycle not in {"review", "deprecated"}:
                    raise SemanticMaintenanceError(f"illegal stale lifecycle: {lifecycle}")
                if page.lifecycle == lifecycle or (
                    lifecycle == "review" and page.lifecycle != "approved"
                ):
                    continue
                edited = _update_frontmatter(markdown, {"lifecycle": lifecycle})
            elif finding.kind == "summary":
                if not finding.summary:
                    raise SemanticMaintenanceError("summary replacement is required")
                if finding.summary == page.summary:
                    continue
                edited = _update_frontmatter(markdown, {"summary": finding.summary})
            else:
                if _CONTRADICTION_MARKER in markdown:
                    continue
                note = finding.note or finding.reason
                note_lines = "\n".join(f"> {line}" for line in note.splitlines())
                callout = (
                    "\n\n> [!warning] Semantic Dream Cycle contradiction\n"
                    f"> Review with: {', '.join(finding.pages)}\n"
                    f"> Reason: {finding.reason}\n{note_lines}\n"
                )
                edited = markdown.rstrip("\n") + callout
            edited = mark_compound_revision(edited, **_revision_routing_fields(self.kb, page_path))
            edits.append((page_path, edited))
        return edits

    def _assemble_unblocked(
        self, store: IngestStore, page_path: str, edited: str
    ) -> IngestProposal | None:
        provenance = SourceProvenance(
            original_filename=page_path,
            content_type="text/markdown",
            converted_by="semantic-dream",
            origin="semantic-dream",
        )
        pipeline = ProposalPipeline(self.kb, store=store)
        proposal = pipeline.assemble(edited, provenance, page_path)
        return None if proposal.blocked else pipeline.stage(proposal)


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
