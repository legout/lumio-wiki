# PRD-0004: Compiled Page Freshness

_Status: approved behavior. Governing decision: ADR-0023. Historical approval originates from the 2026-08-24 implementation review, but its exact reference, revision, and capture checkpoint were not recorded; new execution requires an approved bounded change or reconciled revision._

## Problem Statement

A published Compiled Page can be `approved` indefinitely while the world changes. Maintainers of a living Knowledge Base have no deterministic answer to "which pages are due for re-review?" — the Dream Cycle surfaces structural problems but nothing temporal, and Retrieval gives no freshness signal to Readers or agents.

## Solution

One optional frontmatter field, `review_after` (ISO 8601 date). A page is due for review when `today >= review_after`; absence means no freshness opinion. Surfaced read-only and advisory in `validate`, `lint`, `dream`, and `status`; mapped losslessly to OKF `stale_after` at the Profile 2 boundary. Never blocks validation, never auto-deprecates.

## User Stories

1. As a **Maintainer**, I want to set `review_after` when publishing a time-sensitive page, so that the Knowledge Base reminds me to re-verify it.
2. As a **Maintainer**, I want `lint` to list due-for-review pages as warnings with page and date, so that I can plan re-review work without enabling the semantic layer.
3. As a **Maintainer**, I want the Dream Cycle to rank due pages alongside Link Candidates, so that one command tells me what needs attention.
4. As a **Maintainer**, I want `status` to show a due-page count, so that freshness is visible in routine diagnostics.
5. As an **Owner**, I want freshness to be advisory only, so that a missed review date never breaks validation or hides a page from Readers.
6. As a **local coding agent**, I want `review_after` exported as OKF `stale_after` (Profile 2), so that OKF consumers see the same freshness signal.
7. As a **Maintainer**, I want an imported OKF `stale_after` to arrive as a proposed `review_after` with a diagnostic, so that foreign freshness claims are reviewed, not silently trusted.

## Implementation Decisions

- Field lives in the canonical Compiled Page frontmatter schema; msgspec decode + one validation rule (parseable ISO date).
- All surfacing is computed at read time from the current date; nothing is stored or materialized per publish.
- Changing `review_after` on a published page is an ordinary reviewed page change.
- CONTEXT.md gains the `review_after` vocabulary entry.

## Acceptance and lean assurance

One validation unit may cover schema, date-boundary, representative maintenance output, and OKF exchange. It must distinguish valid/absent/malformed `review_after`, prove the exact due-date boundary stays advisory, and preserve `review_after` ↔ `stale_after` with an import diagnostic. Reuse one due/fresh fixture and existing command journeys; byte-identical gold output or one new test per command is not required.

## Out of Scope

- Per-Claim freshness.
- Automatic lifecycle transitions on due dates.
- Computed freshness scores.
- Reminder/notification delivery beyond the CLI reports.
