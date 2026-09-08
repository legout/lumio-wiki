# 02 — Publication and validation integrity

**P1–P4 implemented; P5 proposed and unexecuted.** Goal: prevent silent canonical data loss and stale
publication while preserving proposal-first local maintenance.
Sources/requirements: [owner decisions and B01–B07](README.md#finding-to-task-map),
[PRD aggregation](../prd/0002-core-sdk.md),
[ontology](../adr/0021-entity-claim-ontology-and-progressive-graph-materialization.md),
[source lifecycle](../adr/0014-source-versions-and-page-level-invalidation.md).
Python/uv, filesystem Markdown and current msgspec records; no new mandatory
storage/locking dependency. All commands below run from the repository root.

One sequential owner: P1/P2 → P3 → P4 → P5. The same owner covers ordinary
publish, moves/compound revisions, repair, Page Removal, Entity Merge, Control
File changes, source registration/retirement/reactivation and discard. There is
no alternate CLI mutation implementation. Global checks: [README](README.md#global-verification-for-future-implementation).

## P1

- [x] **Aggregate Claim diagnostics instead of losing assertions (B06/B07).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/knowledge_base.py`;
  `packages/lumio-wiki/tests/test_entity_claims.py`;
  `packages/lumio-wiki/tests/test_qa_validation_report.py`.
  **Consumes → produces:** raw frontmatter through `_as_claims`/`_load_page` →
  typed pages plus field/file-specific `ValidationIssue`s returned by the
  common loader and publication validation. Prerequisite: retained test floor.
  Propagate parser issues once, without reparsing Claims in a second validator.
  Malformed list/entry/predicate/evidence structures cannot vanish into a valid
  report. Nonnumeric or overflowing confidence becomes an issue, not an uncaught
  conversion error. Reject mapping/list/date/datetime literal values outside
  string/number/boolean kinds, non-string predicate/section scalars, and
  zero/negative/reversed/out-of-body anchors; distinguish `None` from zero.
  Valid Claims still load, lifecycle rules still apply and multiple invalid
  pages contribute diagnostics to the same report.
  **Obligation:** `new-test`; surviving ontology tests miss these parser branches.
  Add `test_malformed_claims_keep_structural_diagnostics`,
  `test_invalid_claim_confidence_is_an_aggregated_issue`,
  `test_container_literal_is_blocked`, and `test_zero_evidence_line_is_blocked`
  beside `test_unknown_predicate_is_blocked` and
  `test_unknown_evidence_section_is_blocked`. Each is one distinct missing
  defect, not a replacement exhaustive schema matrix.
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q packages/lumio-wiki/tests/test_entity_claims.py -k 'malformed_claims_keep_structural_diagnostics or invalid_claim_confidence_is_an_aggregated_issue or container_literal_is_blocked or zero_evidence_line_is_blocked'
  uv run python -m pytest -q packages/lumio-wiki/tests/test_entity_claims.py packages/lumio-wiki/tests/test_qa_validation_report.py
  ```

  **Done:** new nodes first expose acceptance/crash, then all pass; the public
  aggregate report blocks candidate publication without hiding authored errors.
  **Evidence:** integrated locally in `eae343a`; seven focused regressions and
  the two-file validation suite pass, followed by `1493 passed, 9 skipped` with
  four workers. Repository-wide Ruff passes. The seven regressions are
  `test_malformed_claims_keep_structural_diagnostics`,
  `test_invalid_claim_confidence_is_an_aggregated_issue`,
  `test_container_literal_is_blocked`, `test_zero_evidence_line_is_blocked`,
  `test_confidence_overflow_is_an_aggregated_issue`,
  `test_evidence_lines_on_empty_body_are_out_of_bounds`, and
  `test_non_string_claim_scalars_are_blocked`.

## P2

- [x] **Resolve destinations before any candidate/live write (B01).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/publish.py`,
  `packages/lumio-wiki/src/lumio_wiki/ingest.py`,
  `packages/lumio-wiki/tests/test_ingest_journey.py`,
  `packages/lumio-wiki/tests/test_page_removal.py`.
  **Consumes → produces:** proposed relative paths and current page identity →
  one checked destination set consumed by candidate validation and application.
  Prerequisite: P1. New `A_B` must not replace `A B` at `a_b.md`; reject duplicate
  targets within one proposal, existing directories/unreadable occupants,
  escaping paths and compound-fallback collisions. Same existing page revisions
  keep their recorded path and identity; v2 Entity ID changes are not implicit
  replacements, and legacy flat pages retain the explicit title/path identity
  behavior. No automatic suffix allocation or hidden removals. Reuse existing
  move collision protections for every write branch.
  **Obligation:** `new-test`; add
  `test_new_page_slug_collision_preserves_existing_page` and
  `test_duplicate_proposed_destinations_are_blocked` in `test_ingest_journey.py`.
  Keep `test_plain_proposal_validates_against_existing_pages` and existing
  removal/move/merge conflict journeys rather than cloning them.
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q packages/lumio-wiki/tests/test_ingest_journey.py
  uv run python -m pytest -q packages/lumio-wiki/tests/test_page_removal.py packages/lumio-wiki/tests/test_entity_merge.py
  ```

  **Done:** the collision repro leaves original bytes and proposal state intact;
  ordinary same-page revisions still publish; conflicting destination sets
  cannot silently delete another page in temporary validation or live apply.
  **Evidence:** integrated locally in `e555374`; the final candidate passed
  `1523 passed, 10 skipped` with four workers. Focused P2, removal, merge,
  maintenance, and managed-ingest suites passed (136 focused tests), and
  repository Ruff plus P2-file format checks passed. Final review confirmed
  candidate/live parity for lexical symlinks, special files, permissions,
  move/removal ownership, and no-partial-write preflights.

## P3

- [x] **One durable lock/reload boundary (B02/B03).**
  **Files:** create `packages/lumio-wiki/src/lumio_wiki/mutation.py` as a small
  internal filesystem critical-section helper; modify
  `packages/lumio-wiki/src/lumio_wiki/source_registry.py`,
  `packages/lumio-wiki/src/lumio_wiki/ingest.py`,
  `packages/lumio-wiki/src/lumio_wiki/proposal_pipeline.py`;
  `packages/lumio-wiki/tests/test_source_lifecycle.py`,
  `packages/lumio-wiki/tests/test_ingest_journey.py`.
  **Consumes → produces:** canonical KB/private-store resource identities → an
  interprocess mutation critical section and freshly loaded durable state.
  Prerequisites: P2; inventory every writer before changing lock ownership.
  Hold the common per-KB lock through read/check/mutate/commit/rollback, not just
  file replacement. Registry-only callers must use the same registry lock;
  external stores shared by KBs require a consistent resource-lock order so
  neither nested calls nor shared registries lose updates/deadlock. Normalize
  equivalent paths, keep lock files out of portable artifacts, use unique temp
  paths, and fail actionably if interprocess locking is unavailable. A threading
  lock or reload immediately before `_commit` is insufficient: mutation must be
  recomputed from the state read **under** the lock. Remove the unbounded proposal
  cache; read terminal status durably on get/transition. In-memory state after
  exceptions must agree with reload. No new authentication/human-approval model.
  **Obligation:** `new-test`; add
  `test_source_registry_process_writers_preserve_successful_updates` and
  `test_discarded_proposal_cannot_publish_from_stale_store`. Use controlled
  subprocess barriers, not sleeps as proof; include already-open separate store
  instances. Retain `test_register_source_write_failure_restores_live_and_reloaded_state`.
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q packages/lumio-wiki/tests/test_source_lifecycle.py packages/lumio-wiki/tests/test_ingest_journey.py
  ```

  **Done:** both successful registrations survive fresh reload; publish versus
  discard has one durable winner, terminal state cannot be resurrected, and
  all registry/proposal mutations use the same documented lock ownership.
  **Evidence:** integrated locally in `67963a9` after cumulative remediation and
  a clean read-only P3 gate. Focused lifecycle/ingest/capture and related
  source/ingest suites passed serially; the final reviewer reran 167 P3 tests
  plus 32 capture tests, Ruff, and format checks. The full suite passed with
  four workers via the active uv environment: `1539 passed, 9 skipped`.
  Complete live-KB rollback across ordinary publish failures remains the
  separate P5 task.

## P4

- [x] **Persist reviewed affected-file/control preconditions (B03/B04).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/ingest.py`,
  `packages/lumio-wiki/src/lumio_wiki/proposal_pipeline.py`,
  `packages/lumio-wiki/src/lumio_wiki/publish.py`;
  `packages/lumio-wiki/tests/test_ingest_journey.py`,
  `packages/lumio-wiki/tests/test_page_removal.py`,
  `packages/lumio-wiki/tests/test_entity_merge.py`.
  **Consumes → produces:** reviewed base bytes/absence and durable proposal
  metadata → checked mutation preconditions and a fully validated current
  candidate. Prerequisite: P3. Capture expected hashes for every replaced,
  removed, repaired and moved-from/to path, including expected target absence;
  capture Control File/ontology/redirect/pin preconditions when consumed or
  changed. Compare them and durable terminal status under P3's lock immediately
  before constructing/applying the candidate. Revalidate the **full** current
  candidate for new alias/entity/Claim conflicts introduced by disjoint edits.
  A disjoint page change with unchanged control/affected inputs is allowed;
  overlapping changes, altered proposal content or terminal status are rejected
  with restage/review guidance, never silently rebased. Existing serialized
  proposals without review preconditions must be inspected/restaged, not trusted
  by guessing a base from current files. Keep preconditions private, not in pages.
  **Obligation:** `new-test`; add
  `test_overlapping_proposal_base_conflict_preserves_newer_content` and
  `test_disjoint_proposals_publish_after_intervening_change`, the latter also
  exercising full-candidate revalidation. Extend existing removal/merge journeys
  for their control/path preconditions instead of creating parallel matrices.
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q packages/lumio-wiki/tests/test_ingest_journey.py packages/lumio-wiki/tests/test_page_removal.py packages/lumio-wiki/tests/test_entity_merge.py
  ```

  **Done:** reviewed overlapping state cannot overwrite newer content; disjoint
  proposals remain publishable; all routes persist/consume the same preconditions.
  **Evidence:** integrated locally in `864d64b` after cumulative remediation and
  a clean read-only P4 gate. Private reviewed identities and exact path/control
  preconditions fail closed across ordinary, removal, merge, and source-lifecycle
  routes; focused P4/P3 suites and the final reviewer checks passed. The full
  suite passed with four workers via the active uv environment: `1569 passed,
  9 skipped`. Complete live-KB rollback across ordinary publish failures remains
  the separate P5 task.

## P5

- [ ] **Rollback ordinary local failures across the complete mutation (B05).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/mutation.py`,
  `packages/lumio-wiki/src/lumio_wiki/publish.py`,
  `packages/lumio-wiki/src/lumio_wiki/proposal_pipeline.py`,
  `packages/lumio-wiki/src/lumio_wiki/ingest.py`,
  `packages/lumio-wiki/src/lumio_wiki/source_registry.py`;
  `packages/lumio-wiki/tests/test_ingest_journey.py`,
  `packages/lumio-wiki/tests/test_source_lifecycle.py`.
  **Consumes → produces:** P4's validated candidate and exact pre-mutation
  files/state → either the complete successful mutation or restored prior state.
  Prerequisites: P3/P4. Reuse the prepared candidate, snapshot existence/bytes of
  touched pages, control, reserved navigation artifacts (and live Activity Log
  until S1), registry/pending transitions and durable proposal metadata. Keep
  backups outside canonical content; restore replacements/deletions, remove new
  files and invalidate disposable derived caches on failure. Registry transition
  and proposal status persistence are inside the boundary; write no published
  marker or print success before all steps succeed. Restoration failures must
  report failure and preserve recovery backups/actionable paths, never claim a
  rollback succeeded. Do not remove successfully retained immutable raw artifacts
  as compensation; retain their existing recoverable-orphan policy.
  **Obligation:** `new-test`; add `test_local_publish_failure_restores_complete_mutation`
  for an injected second-page live write failure, and extend
  `test_source_transition_persistence_failure_rolls_back_terminal_proposal` plus
  `test_terminal_proposal_persistence_failure_keeps_reactivation_state_unchanged`
  to compare the whole affected byte/state set after late failures.
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q packages/lumio-wiki/tests/test_ingest_journey.py packages/lumio-wiki/tests/test_source_lifecycle.py packages/lumio-wiki/tests/test_page_removal.py packages/lumio-wiki/tests/test_entity_merge.py tests/test_kb_control.py
  ```

  **Done:** early and late ordinary failures restore the complete mutation and
  release locks; success state is last; existing navigation/source privacy holds.

## Limits and review gate

The audit's passing suite did not cover these failures. Reproduce each before
fixing it. Cooperating multi-process writers are serialized, but arbitrary
editors can ignore advisory locks; detect known affected-path drift and document
remaining external-edit races. File-by-file deployment and restoration may be
visible to readers. Process termination, power loss, disk failure preventing
restoration and atomic reader visibility require stronger infrastructure and are
**not** promised here. Do not describe this as crash-atomic publication or a
private-app transaction guarantee. Mutation behavior/API changes must also pass
[S2/S4 consumer review](05-simplification-release.md) before release.
