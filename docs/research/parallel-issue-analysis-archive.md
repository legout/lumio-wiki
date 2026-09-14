# Archived parallel issue implementation analysis

_Date: 2026-07-15. Historical evidence only. These issues and private-application paths predate ADR-0025 and the current planning contract; do not dispatch from this snapshot._

This was a dependency-aware planning snapshot for the former combined repository. Current work must be re-derived from approved behavioral sources, live tracker state, stable interfaces, and non-conflicting ownership.

## Safest immediate batch

The strongest current parallel batch is:

- **#52 — Chat history: Continue threads with bounded fresh-grounded context**
  - Separate chat-history/runtime track.
  - Use a separate worktree because `app.py` and `ui.py` are shared hotspots.
- **#66 — Navigation Index: Carry generated catalogs through every Storage Mode**
  - Primarily storage backends and storage tests.
- **#67 — OKF Profile 1: Export a public Knowledge Base through the existing client seam**
  - Primarily a new OKF exchange module plus export endpoint wiring.

Issues #66 and #67 were sibling issues under #63 and were considered the safest pair to run concurrently. Issue #52 could run alongside them in its own worktree, with normal integration care around `app.py`.

## Follow-on parallel groups

After **#52** merges:

- **#53 — Support explicit threads for external clients**
- **#54 — Add Temporary Chat without persistence**
- **#55 — Recall prior Reader questions lexically**

These are dependency siblings. They share chat routing/UI surfaces, so use separate worktrees and an integration pass rather than editing a shared checkout.

After **#55** merges, **#56, #57, #58, and #59** can fan out by dependency, but they share recall/history persistence code and are therefore merge-conflict-prone. **#61** follows #57/#58/#59, and **#62** is the final journey/polish issue.

After **#67** merges:

- **#68** and **#69** can proceed in parallel by dependency graph.
- **#70** follows #69.
- **#71** follows #68 and #69.
- **#72** is the final integration journey.

After **#78** merges, **#79** and **#80** are dependency siblings; **#81** follows them. They share ingestion/publish code, so a single owner or tightly coordinated worktrees are preferable.

## Tracks better kept serial

- **Chat sources:** #34, #35, and #37 are conceptually parallel, but share `app.py`, `ui.py`, and chat-source tests. Prefer one owner or explicit file ownership.
- **Reading Room:** #45 and #47 are dependency siblings, but both concentrate changes in `ui.py`; serialize where possible, followed by #48.
- **Category-aware ingestion:** #77 → #78 → (#79/#80) → #81. It extends the canonical reserved-artifact and publish model and should not naively run alongside Navigation/OKF core changes.

## Do not duplicate active or non-leaf work

- **#35** was the current branch (`feat/chat-pdf-docx-sources-35`) with uncommitted changes in `src/lumio/ui.py`, `tests/test_chat_sources.py`, and `tests/test_conversation_source_processors.py`; inspect and preserve that work before dispatching another agent.
- **#34, #45, and #47** were assigned to `legout` at the time of analysis.
- **#32, #41, #49, #63, and #76** are umbrella/specification issues rather than ideal leaf implementation assignments.
- **#73, #74, and #75** still require triage.

## Historical dispatch recommendation

The original recommendation used one implementer per leaf issue and an integration pass. It is retained only as evidence; ADR-0027 and `AGENTS.md` now govern dispatch, validation units, review, and candidate assembly.
