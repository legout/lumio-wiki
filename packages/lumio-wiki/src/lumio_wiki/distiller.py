"""Distiller: convert normalized material into proposed Compiled Page Markdown
(issue #95, AC2; canonical owner moved into ``lumio-wiki`` by issue #97;
OpenAI-compatible unattended Distiller added by issue #101).

A Distiller is the second stage of ingestion. It turns a :class:`NormalizedSource`
into the Markdown a proposal is built from. The base interface carries no model
provider: the host coding agent may act as the Distiller
(:class:`PassthroughMarkdownDistiller`). An OpenAI-compatible Distiller may
either be an adapter over the full application's provider
(``lumio.distiller.ProviderDistiller``) or the unattended
:class:`OpenAIDistiller` shipped behind the ``lumio-wiki[llm]`` extra. Both
keep the base wheel model-free (ADR-0010): the unattended Distiller imports
the OpenAI SDK lazily and raises an actionable, extra-naming error when the
extra is absent (issue #101, AC1/AC4, PRD user story 18).
"""

from __future__ import annotations

import importlib
import time
from typing import Any, Protocol, runtime_checkable

from lumio_wiki.source_processor import NormalizedSource


class OpenAIDistillerError(RuntimeError):
    """Raised when the unattended OpenAI-compatible Distiller fails actionably.

    Every message is phrased so a Maintainer knows what to do: install the
    missing extra, inspect a transient provider outage, or correct malformed
    model output. The error never carries raw model bytes or provider headers.
    """


# Default retryable provider error names. The unattended Distiller cannot import
# the OpenAI SDK at module load (the base wheel must stay model-free), so it
# retries by exception *type name* rather than by ``isinstance``. This matches
# the SDK's own retry surface (connection/timeout/rate-limit) while staying
# resilient to SDK version drift and staying importable without ``openai``.
_DEFAULT_RETRYABLE_ERRORS: tuple[str, ...] = (
    "APIConnectionError",
    "APITimeoutError",
    "RateLimitError",
    "InternalServerError",
    "ConnectionError",
    "TimeoutError",
)

# Actionable hint naming the exact install command (PRD user story 18). Quoted
# so it works on POSIX shells and PowerShell; included verbatim in the error.
_LLM_EXTRA_HINT = (
    "the lumio-wiki[llm] extra is required for unattended distillation; "
    "install it with:  pip install 'lumio-wiki[llm]'"
)

# The system prompt mirrors the full application's distill prompt so an
# unattended Distiller produces the same shape of Markdown (page-break split,
# required frontmatter fields, category-aware routing) the proposal pipeline
# expects. Synthesis routing guidance is emitted only when ``synthesis`` is one
# of the configured Content Categories (issue #78, P2.7) — a custom-catalog
# Knowledge Base only ever sees its own categories offered.
_DISTILL_SYSTEM_PROMPT_TEMPLATE = (
    "You are Lumio's ingest assistant. Convert the provided raw source text into one or "
    "more Compiled Page Markdown files, each with valid YAML frontmatter. Split the source "
    "into as many pages as it naturally contains, separating each page "
    "with a line containing exactly: <!-- lumio: page-break -->. Required frontmatter "
    "fields per page: title (string){category_clause}, type (a non-empty free-form type "
    "describing the page's specific semantics, e.g. 'definition', 'organization profile', "
    "'runbook', 'dataset descriptor'), durability_rationale (one sentence on why this page "
    "is durable knowledge worth maintaining), lifecycle (draft/review/approved/deprecated), "
    "visibility (public/internal/restricted), tags (list of strings), and sources (list "
    "with id and title). Optional fields: aliases, summary, relationships, synthetic. "
    "{synthesis_clause}Do not invent facts not present in the source text."
)


@runtime_checkable
class Distiller(Protocol):
    """Convert normalized material into proposed Compiled Page Markdown."""

    def distill(
        self, normalized: NormalizedSource, *, categories: list[str] | None = None
    ) -> str: ...


class PassthroughMarkdownDistiller:
    """Model-free Distiller: the normalized text is already authored Markdown.

    Enables text and Markdown Knowledge Sources to reach a reviewable Ingest
    Proposal without an OpenAI-compatible provider (issue #95, AC4). The host
    coding agent authors the Compiled Page Markdown; this Distiller passes the
    normalized text through unchanged so downstream page extraction, summary
    generation, and category routing apply as they do for any distilled source.
    """

    def distill(
        self, normalized: NormalizedSource, *, categories: list[str] | None = None
    ) -> str:
        return normalized.text


class OpenAIDistiller:
    """Unattended OpenAI-compatible Distiller (issue #101, ``lumio-wiki[llm]``).

    Implements the shared :class:`Distiller` interface so provider-driven
    ingestion feeds the same :class:`~lumio_wiki.proposal_pipeline.ProposalPipeline`
    as host-agent and document-driven ingestion: provenance, category/type
    routing, durability rationale, validation, diff, and blast radius are
    identical (AC3). The base ``lumio-wiki`` wheel never imports the OpenAI SDK
    at module load — ``openai`` is imported lazily inside ``__init__`` (when no
    client is injected) so the model-free invariant holds (AC1, ADR-0010).

    Construction:

    - ``client`` may be injected (tests, custom transports, a cached
      connection). When it is ``None``, the Distiller constructs one from
      ``base_url`` / ``api_key`` via the installed OpenAI SDK; if the SDK is
      absent it raises :class:`OpenAIDistillerError` naming the exact extra to
      install (AC4, PRD user story 18).
    - ``max_retries`` / ``backoff_seconds`` govern the structured-output retry
      behavior: transient provider errors (connection, timeout, rate-limit) are
      retried with linear backoff; a non-retryable error surfaces immediately
      (AC2).

    The Distiller never makes a network call at construction time — only inside
    :meth:`distill`. Tests inject a fake client so no real API call is ever
    made (AC6).
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        client: Any | None = None,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        retry_errors: tuple[str, ...] | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.model = model
        self._base_url = base_url
        self._api_key = api_key
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._retry_error_names = (
            retry_errors if retry_errors is not None else _DEFAULT_RETRYABLE_ERRORS
        )
        self._client = client if client is not None else self._build_client()

    def _build_client(self) -> Any:
        """Construct the OpenAI SDK client lazily (AC1: never imported at module load).

        Raises :class:`OpenAIDistillerError` with the actionable install hint
        when the ``[llm]`` extra is not installed (AC4).
        """
        try:
            openai_module = importlib.import_module("openai")
        except ImportError as exc:
            raise OpenAIDistillerError(
                f"cannot build the OpenAI client: {_LLM_EXTRA_HINT}"
            ) from exc
        kwargs: dict[str, Any] = {}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        if self._api_key:
            kwargs["api_key"] = self._api_key
        return openai_module.OpenAI(**kwargs)

    def distill(
        self, normalized: NormalizedSource, *, categories: list[str] | None = None
    ) -> str:
        """Distill normalized source text into proposed Compiled Page Markdown.

        Same input/output shape as :meth:`PassthroughMarkdownDistiller.distill`
        so the two Distillers are interchangeable at the proposal pipeline seam.
        Retries transient provider errors and surfaces actionable errors on
        empty model output or exhausted retries (AC2).
        """
        system_prompt = _build_distill_system_prompt(categories)
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Source filename: {normalized.filename or 'unknown'}\n\n"
                    f"{normalized.text}"
                ),
            },
        ]
        content = self._call_with_retry(messages)
        if not content or not content.strip():
            raise OpenAIDistillerError(
                "provider returned empty content for the distilled page; "
                "check the model response or provider configuration"
            )
        return content

    def _call_with_retry(self, messages: list[dict[str, str]]) -> str | None:
        """Call the provider with structured-output retry/error behavior (AC2).

        Transient errors (by configured type name) are retried up to
        ``max_retries`` times with linear backoff; any other error surfaces
        immediately. Returns the assistant message ``content`` (may be ``None``
        if the model returned no content — :meth:`distill` converts that into an
        actionable error).
        """
        last_exc: Exception | None = None
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                )
            except Exception as exc:  # noqa: BLE001 - provider surface is open
                last_exc = exc
                if self._is_retryable(exc) and attempt < attempts - 1:
                    time.sleep(self._backoff_seconds * (attempt + 1))
                    continue
                raise OpenAIDistillerError(
                    f"provider request failed after {attempt + 1} attempt(s): {exc}"
                ) from exc
            choice = _first_choice(response)
            if choice is None:
                raise OpenAIDistillerError(
                    "provider returned no completion choice; "
                    "check the model response or provider configuration"
                )
            message = getattr(choice, "message", None)
            return getattr(message, "content", None) if message is not None else None
        # Unreachable: the loop either returns or raises. Kept for type safety.
        raise OpenAIDistillerError(
            f"provider request failed: {last_exc}"
        )  # pragma: no cover

    def _is_retryable(self, exc: Exception) -> bool:
        """Return whether ``exc`` is a configured retryable provider error.

        The base wheel cannot import the OpenAI SDK, so retryability is decided
        by exception type *name* rather than ``isinstance``. Callers needing
        exact-type retry may pass ``retry_errors`` (e.g. a test's own transient
        error class).
        """
        if type(exc).__name__ in self._retry_error_names:
            return True
        return any(
            type(base).__name__ in self._retry_error_names
            for base in type(exc).__mro__[1:]
        )


def _first_choice(response: Any) -> Any:
    """Return the first completion choice from an OpenAI-style response, or ``None``."""
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    try:
        return choices[0]
    except (IndexError, TypeError):
        return None


def _build_distill_system_prompt(categories: list[str] | None) -> str:
    """Build the distill system prompt offering exactly the configured categories.

    ``categories`` is the Knowledge Base's configured Content Category catalog
    (``None`` for Legacy Flat Mode). The prompt lists only those categories —
    never a hard-coded catalog — so a custom-catalog KB only sees its own
    categories offered (issue #78, P2.7). Synthesis routing guidance is included
    only when ``synthesis`` is configured.
    """
    if categories:
        catalog = ", ".join(categories)
        category_clause = (
            f", category (one of the configured Content Categories: {catalog})"
        )
        if "synthesis" in categories:
            synthesis_clause = (
                "Use category 'synthesis' only for cross-source or cross-page "
                "conclusions, citing multiple sources or relationships; do not set "
                "synthetic based on category (synthetic is reserved for pages derived "
                "from other compiled knowledge). "
            )
        else:
            synthesis_clause = ""
    else:
        category_clause = ""
        synthesis_clause = ""
    return _DISTILL_SYSTEM_PROMPT_TEMPLATE.format(
        category_clause=category_clause,
        synthesis_clause=synthesis_clause,
    )


__all__ = [
    "Distiller",
    "OpenAIDistiller",
    "OpenAIDistillerError",
    "PassthroughMarkdownDistiller",
]
