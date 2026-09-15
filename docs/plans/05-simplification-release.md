# 05 — Simplification and coordinated release

**Proposed; no integrated completion is recorded and implementation is not approved by this revision.** Goal: satisfy
[PRD-0006 AC4](../prd/0006-library-stabilization.md#ac4-simplification-and-coordinated-release)
without removing supported capabilities or blindly breaking external consumers.
Evidence: [B19–B21 and C01–C04](../research/library-stabilization-audit.md#finding-registry)
and the [public/private consumer API inventory](../research/plan-05-consumer-api-inventory.md).
Architecture: [ADR-0022](../adr/0022-retire-activity-log-artifact.md),
[ADR-0025](../adr/0025-repository-split.md),
[Core PRD small seam](../prd/0002-core-sdk.md), and
[progressive graph](../adr/0021-entity-claim-ontology-and-progressive-graph-materialization.md).
Python/uv, two public wheels; no mandatory dependencies or new generic engine.

Approval reference: the 2026-09-15 owner selection “Fix plan + inventory”
authorized only the stale-validation reconciliation and read-only consumer
inventory. The owner later accepted that inventory as evidence, chose a
private-first Activity Log migration, and confirmed ADR-0022's producer-API
deletion after verified migration of the two known repositories, explicitly
accepting unknown external-consumer breakage risk. The owner approved the
bounded S1a design **for documentation only**. No S1–S4 implementation,
removal, further private-repository mutation, or release is authorized. Capture
checkpoint: no new product vocabulary or ADR; ADR-0022 already owns the
retirement and API deletion, PRD-0006 owns stabilization behavior and non-goals,
and the private-first choice changes only execution decomposition.
Private documentation was reconciled to ADR-0022 in `legout/lumio` merge
`43facf97fc40497688b54546b511c5bd392396a6` without changing its active writer.
Migration evidence remains unresolved, so all implementation and removal gates
remain blocked before dispatch. Contract version 1; local project skill
provenance `unknown`.

Sequence: S1a migrates and verifies the private writer and direct API consumers
first; S1b then stops public production, centralizes filesystem/S3 legacy-log
omission, and deletes producer APIs per ADR-0022. Inventory acceptance alone is
not deletion authority; the exact reviewed S1a revision is required. S2 then
migrates other seams in coherent batches. S3 consumes R1 and approved traversal
semantics. S4 remains separately approved release work.

## Validation units

| Unit | Tasks | Risk / obligation | Distinct failure protected |
| --- | --- | --- | --- |
| SV1a | S1a | high / `new-test` | the private writer survives, legacy logs leak into a new version, or publish/audit atomicity regresses |
| SV1b | S1b | high / `new-test` | the public writer survives or centralized pruning changes legacy recognition/collision safety |
| SV1c | S2–S3 | high / `existing-check` | a real consumer breaks or traversal authorization/topology changes |
| SV2 | S4 | high / `existing-check` | built wheels, optional capabilities, or an adopted consumer fail at the release boundary |

SV1a and SV1b require focused red-green evidence for the new no-copy invariant.
SV1c uses existing public journeys. Each consumed interface boundary receives
immediate review plus one exact-candidate review. SV2 reuses release and consumer
checks; release notes do not create a second documentation-only validation unit.

## S1

- [ ] **S1a — Stop the private Activity Log writer and migrate its direct test consumers (design approved; implementation not authorized).**
  **Files in `legout/lumio`:** `packages/lumio/src/lumio/app.py`;
  `tests/test_publish.py`, `tests/test_compound_ingest.py`,
  `tests/test_external_import_publish.py`, `tests/test_okf_complete_journey.py`,
  plus focused regression coverage in `tests/test_page_removal.py`,
  `tests/test_proposal_pipeline.py`, and `tests/test_audit_log.py`.
  **Consumes → produces:** the current candidate publication flow and accepted
  ADR-0022 contract → private publications that preserve Navigation Indexes, Hot
  Index, Published Version records, atomic rollback, index freshness handling,
  and private SQLite audit events while producing, appending, or copying no
  portable Activity Log.
  **Prerequisites:** accepted inventory evidence and private documentation merge
  `43facf97fc40497688b54546b511c5bd392396a6` are complete. A new owner approval
  is still required before any implementation dispatch or private source/test
  mutation.
  **Implementation boundary:** remove the two producer imports
  (`append_activity_log_entry` and `make_activity_log_entry`), retain
  `publish_reserved_artifacts`, and remove the Activity-Log-only description
  helper and append block from `_publish_pages`. Before copying the live tree or
  opening any candidate Markdown, run a local transitional no-follow walk over
  every root, nested, and case-variant `log.md`; require each occupant to be a
  regular non-symlink file and reject directories, symlinks, FIFOs, sockets, and
  other special entries without opening or traversing them. Immediately after
  `publish_reserved_artifacts(candidate_path)` validates all marked-content
  collisions, remove every collected legacy-log path from the candidate before
  `atomic_replace_transaction`. At that point each is a validly marked legacy
  artifact; malformed or unmarked regular files have already blocked. This
  private preflight/pruning is transitional until S1b centralizes the invariant.
  Immutable older Published Versions are never edited.
  **Validation unit:** SV1a. Add focused coverage that a fresh categorized
  publication creates no log and that a candidate containing valid marked root
  and nested/case-variant legacy logs publishes without carrying them forward.
  Add a bounded parameterized no-follow matrix covering root, nested, and
  case-variant names against symlink, FIFO, socket, and directory occupants;
  prove rejection precedes candidate copying/opening and leaves every entry and
  external referent untouched.
  Update direct producer-API tests to assert their real navigation, publication,
  and provenance outcomes without manually creating `log.md`. Correct stale
  journey prose. Keep legacy marked-log exclusion and unmarked/malformed
  collision tests unchanged.
  **Verify:**

  ```sh
  cd /home/volker/coding/lumio
  uv run pytest -q tests/test_publish.py tests/test_page_removal.py tests/test_proposal_pipeline.py tests/test_compound_ingest.py tests/test_external_import_publish.py tests/test_okf_complete_journey.py tests/test_audit_log.py
  uv run ruff check packages/lumio/src tests/test_publish.py tests/test_compound_ingest.py tests/test_external_import_publish.py tests/test_okf_complete_journey.py
  uv run ruff format --check packages/lumio/src tests/test_publish.py tests/test_compound_ingest.py tests/test_external_import_publish.py tests/test_okf_complete_journey.py
  uv run pytest -q -n 4
  ! rg -n 'append_activity_log_entry|make_activity_log_entry|_format_activity_log_line' packages/lumio/src tests
  ```

  **Done:** the private production tree has no producer-symbol import/call; its
  direct tests no longer consume the producer API; fresh and legacy-bearing
  publications contain no `log.md`; navigation, version, rollback, index, and
  private-audit evidence passes; exact reviewed private commit is recorded.

- [ ] **S1b — Stop public Activity Log production and centralize legacy-log pruning (proposed; blocked on S1a and separate approval).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/proposal_pipeline.py`,
  `packages/lumio-wiki/src/lumio_wiki/knowledge_base.py`,
  `packages/lumio-wiki/src/lumio_wiki/records.py`,
  `packages/lumio-wiki/src/lumio_wiki/__init__.py`,
  `packages/lumio-wiki/src/lumio_wiki/publish.py`,
  `packages/lumio-wiki/src/lumio_wiki/s3_publish.py`, `README.md`,
  `scripts/verify_lumio_wiki_wheel.py`;
  `tests/test_kb_control.py`, `packages/lumio-wiki/tests/test_entity_merge.py`,
  `packages/lumio-wiki/tests/test_page_removal.py`,
  `packages/lumio-wiki/tests/test_ingest_journey.py`, and
  `packages/lumio-wiki/tests/test_s3_publish.py`.
  **Consumes → produces:** verified S1a revision and recognized legacy artifacts
  → no public writer or producer API, plus one internal post-validation rule
  that omits valid marked legacy logs from both filesystem and S3 publications.
  Keep `canonical_content` complete for captured snapshot/loading identity; apply
  the omission only while preparing a new Published Version.
  **Prerequisites:** S1a integration evidence and explicit public implementation
  approval. Remove public production append calls; delete `ActivityLogEntry`,
  `append_activity_log_entry`, `make_activity_log_entry`, formatting helpers,
  and their public exports per ADR-0022. Retain only internal legacy marker and
  version constants needed for recognition, collision checks, and fingerprint
  exclusion.
  **Shared safety/omission boundary:** add one internal Knowledge Base collector
  that performs a no-follow walk; it returns sorted regular relative paths for
  every root, nested, and case-variant `log.md`, plus blocking issues for
  directories, symlinks, FIFOs, sockets, or other special entries, without
  opening or traversing them. Integrate it at the start of exported
  `validate_candidate_knowledge_base`, before `_copy_candidate_tree`: unsafe
  results return a blocking `ValidationReport` and skip the copy entirely, so
  proposal assembly and `graph_exchange` callers receive the normal validation
  contract instead of reading a referent or leaking a filesystem exception.
  Publication entrypoints invoke the same collector immediately before their own
  mutation/capture boundary and map issues to their existing public error type;
  after normal marker validation, that operation's returned paths drive
  filesystem removal or S3 mapping omission. Do not cache paths across calls:
  re-collect under the applicable cooperating-writer/publication boundary so a
  stale validation result is never used for pruning.
  **Filesystem/rollback boundary:** `ProposalPipeline` must run the collector
  before candidate validation and include the Activity Log basename in its
  pre-mutation basename-sweep backup and rollback removal set, replacing the
  current root-only guard/capture. A failure after pruning restores all previous
  variants and removes untracked variants. Direct reserved-artifact publication
  runs the same collector before loading Markdown and removes validated paths
  before a new version commits.
  **S3 boundary:** run the preflight against `source_root` before
  `_capture_source`; after validating the captured source, omit the collected
  paths from `canonical_content(source)` before canonical objects and the
  manifest are written. The source snapshot itself is not mutated.
  **Validation unit:** SV1b. Add a bounded parameterized no-follow matrix covering
  root, nested, and case-variant names against symlink, FIFO, socket, and
  directory occupants; direct `validate_candidate_knowledge_base` coverage must
  prove a blocking report is returned before `_copy_candidate_tree`, and each
  publication surface must prove rejection precedes any open/capture and leaves
  entries/referents untouched. Add valid-log no-copy coverage for filesystem and
  S3 publication, plus a `test_ingest_journey.py` regression
  that fails after pruning and proves the complete legacy-log set is restored.
  Update retained atomic merge/removal tests to assert successful
  mutation/navigation without generated or appended logs. Keep loader
  recognition, fingerprint exclusion, S3 reads of historical versions, and
  blocking unmarked/malformed collision coverage. Revise the isolated-wheel
  verifier to assert regenerated publication artifacts contain no `log.md`
  instead of importing deleted producer APIs.
  **Verify:**

  ```sh
  uv run pytest -q tests/test_kb_control.py packages/lumio-wiki/tests/test_entity_merge.py packages/lumio-wiki/tests/test_page_removal.py packages/lumio-wiki/tests/test_ingest_journey.py packages/lumio-wiki/tests/test_s3_publish.py
  uv run python -m ruff check packages tests eval scripts
  uv run python -m ruff format --check packages tests eval scripts
  ! rg -n 'ActivityLogEntry|append_activity_log_entry|make_activity_log_entry|_format_activity_log_line' packages/*/src scripts/verify_lumio_wiki_wheel.py
  uv build --package lumio-wiki --wheel --out-dir dist/lumio-wiki
  uv venv --clear --python 3.14 /tmp/lumio-wiki-plan05-s1b-venv
  uv pip install --python /tmp/lumio-wiki-plan05-s1b-venv/bin/python dist/lumio-wiki/lumio_wiki-*.whl
  /tmp/lumio-wiki-plan05-s1b-venv/bin/python scripts/verify_lumio_wiki_wheel.py dist/lumio-wiki/lumio_wiki-*.whl tests/fixtures/valid tests/fixtures/categorized_kb
  ```

  **Done:** no public producer definition, export, or caller remains; all new
  filesystem/S3 publications omit valid legacy logs; handled failure restores the
  complete pre-mutation log set; immutable old versions, recognition, collision
  blocking, fingerprints, S3 historical reads, and both navigation artifacts
  remain intact.

## S2

- [ ] **Consume the approved inventory; migrate and narrow duplicated seams (B20/B21/C02/C04).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/__init__.py`,
  `packages/lumio-wiki/src/lumio_wiki/location.py`,
  `packages/lumio-wiki/src/lumio_wiki/knowledge_base.py`,
  `packages/lumio-wiki/src/lumio_wiki/ingest.py`,
  `packages/lumio-wiki/src/lumio_wiki/embeddings.py`,
  `packages/lumio-wiki/src/lumio_wiki/source_processor.py`,
  `packages/lumio-wiki/src/lumio_wiki/retrieval_eval.py`,
  `packages/lumio-wiki/src/lumio_wiki/cli.py`,
  `packages/lumio-wiki/src/lumio_wiki/composition.py` (created by A3),
  `packages/lumio-lancedb/src/lumio_lancedb/__init__.py`,
  `packages/lumio-lancedb/src/lumio_lancedb/graph.py`,
  `docs/packaging.md`, `docs/usage.md`, `README.md`;
  `tests/test_core_sdk_index.py`, `tests/test_graph_traversal.py`,
  `packages/lumio-wiki/tests/test_location_snapshot.py`.
  **Consumes → produces:** an approved in-repo and external consumer import/call
  inventory → a documented primary workflow surface, named secondary owners,
  and a migration ledger with verified consumer revisions/checks. The owner
  accepted the current
  [inventory evidence](../research/plan-05-consumer-api-inventory.md) as the
  evidence baseline on 2026-09-15; migration and removal remain separately gated.
  Prerequisites: the inventory exists as accepted evidence before dispatch;
  actual removals follow P1–P5/R1–R5/A3–A6 interface stabilization. Include
  private `legout/lumio` and any other confirmed consumers supplied by their
  owners, not hypothetical compatibility users. Without that evidence, exported
  deletion and release remain blocked. No private repo writes are authorized.
  Review root parser aliases (`parse_frontmatter`, `as_sources`), low-level
  constants/display helpers, `DocumentSourceProcessor`, `cosine_similarity`,
  `is_semantic_index_stale`, snapshot/SDK duplicate traversal, `load_graph_state`
  and old adapter-discovery helpers. These are **candidates**, not proven dead
  APIs. Both CLI search printers and entity candidate search have real callers.
  Migrate known consumers to the chosen existing owner, verify their behavior,
  then remove redundant root aliases/implementations/cache paths. P3 removes the
  stale proposal cache; A3 owns ingestion/composition deduplication; do not redo
  them. Any temporary compatibility bridge names its actual consumer and ends
  when that recorded revision migrates, within the coordinated breaking release.
  **Validation unit:** SV1c. Use load/search/cited retrieval, ingest, remote
  adapter, and installed journeys; do not resurrect exhaustive export
  inventories.
  **Verify migration and focused behavior:**

  ```sh
  rg -n 'from lumio_wiki|import lumio_wiki|from lumio_lancedb|import lumio_lancedb' packages tests eval scripts examples
  rg -n 'parse_frontmatter|as_sources|graph_path|load_graph_state|DocumentSourceProcessor|cosine_similarity|is_semantic_index_stale|load_lancedb_adapter' packages tests eval scripts examples
  uv run pytest -q tests/test_core_sdk_index.py tests/test_graph_traversal.py packages/lumio-wiki/tests/test_location_snapshot.py packages/lumio-wiki/tests/test_ingest_journey_isolation.py packages/lumio-wiki/tests/test_cli_remote_lance.py packages/lumio-lancedb/tests/test_remote_index_binding.py
  ```

  **Done:** migration ledger names each removed interface's actual replacement
  and consumers/checks; no stale production caller remains. External verification
  commands are recorded from the real consumer repository before approval, not
  fabricated here. Preserve optional features, raw-source isolation, distinct
  page search/Evidence retrieval and converter provenance/citation semantics.

## S3

- [ ] **One bounded traversal owner over captured facts; disposable caches (C03/B20).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/knowledge_base.py`,
  `packages/lumio-wiki/src/lumio_wiki/graph_state.py`,
  `packages/lumio-wiki/src/lumio_wiki/location.py`,
  `packages/lumio-lancedb/src/lumio_lancedb/graph.py`;
  `tests/test_graph_traversal.py`,
  `packages/lumio-wiki/tests/test_graph_state.py`,
  `packages/lumio-wiki/tests/test_graph_retrieval.py`,
  `packages/lumio-lancedb/tests/test_ontology_parity.py`.
  **Consumes → produces:** R1 captured page/index/GraphState facts → one owned
  authorization-before-expansion, bounded traversal path, with source-matched
  MessagePack/LanceDB projections as disposable caches.
  Prerequisites: R1 and S2 consumer inventory. First retain the index already
  built during validation instead of deriving it again. Then route supported
  loaded graph facts through one traversal owner with an explicit existing-seam
  injection/load path; validate fingerprint, scope, origin/lifecycle and eligible
  page identity before use. `graph_path` and `shortest_path` differ in aliases,
  missing/same endpoints and bounds: migrate actual consumers to the selected
  bounded behavior, not a blind alias. Keep cycle safety, hidden-intermediate
  exclusion, empty seed non-broadening and edge budgets including parallel edges.
  Do not merge Hot/Navigation Indexes or delete either accepted derived format.
  Measure extraction/startup/memory/traversal before claiming speed improvements;
  the audit established duplication, not a measured scale regression. No generic
  GraphStore, new graph database or universal engine.
  **Validation unit:** SV1c. Retain
  `test_legacy_graph_path_still_works`,
  `test_rebuilt_graph_reproduces_public_traversal_results`, graph eligibility
  journeys and ontology parity; migrate legacy caller tests only with the agreed
  semantics, never weaken authorization/bounds for compatibility.
  **Verify:**

  ```sh
  uv run pytest -q tests/test_graph_traversal.py packages/lumio-wiki/tests/test_graph_state.py packages/lumio-wiki/tests/test_graph_retrieval.py packages/lumio-lancedb/tests/test_ontology_parity.py
  rg -n 'def graph_path|def shortest_path|_KnowledgeIndex\(pages\)' packages/lumio-wiki/src/lumio_wiki
  ```

  **Done:** one implementation owns traversal, delegates/caches have truthful
  snapshot identity, retained topology/security/budget tests pass and abandoned
  derivation/BFS paths are gone. Record measurement if claiming a performance win.

## S4

- [ ] **Separately approve and certify the breaking release/consumer migration.**
  **Files:** `docs/packaging.md`, `README.md`,
  `packages/lumio-wiki/pyproject.toml`, `packages/lumio-lancedb/pyproject.toml`,
  `uv.lock`, `packages/lumio-wiki/src/lumio_wiki/data/skill/SKILL.md`;
  create `docs/release-notes/coordinated-api-migration.md`.
  Read the existing `.github/workflows/ci.yml`,
  `.github/workflows/lumio-lancedb-wheel.yml`, `.github/workflows/release.yml`,
  `scripts/verify_lumio_wiki_wheel.py`, and
  `packages/lumio-lancedb/tests/test_lancedb_adapter.py`; no CI redesign implied.
  **Consumes → produces:** approved S2 migration ledger and checked fixes →
  explicitly approved lockstep public wheel pair, compatibility ranges,
  release notes/skill version and private-consumer adoption evidence.
  Prerequisites: new owner release approval; all P/R/A tasks and S1–S3 complete;
  private application migration certified by its owner. Confirm supported
  consumers and select actual version/range according to the release policy
  then, not an invented number in this unexecuted plan. Align public foundation
  vocabulary/scope in docs while leaving private UI/runtime governance with the
  application. Document old-proposal restaging, snapshot freshness behavior,
  API removals, optional integration and ordinary-failure/crash limits.
  **Validation unit:** SV2. Reuse installed-wheel and consumer-adoption checks;
  add no test for release notes. The retained suite passes once with four workers,
  and independent `[documents]`, `[llm]`, `[all]`, base, and S3/adapter checks
  remain release evidence.
  Run from an approved candidate checkout; these commands use disposable venvs:

  ```sh
  set -eu
  uv lock --check
  uv run pytest -q -n 4
  uv run python -m ruff check packages tests eval scripts
  git diff --check
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  uv build --package lumio-wiki --wheel --out-dir "$tmp/wiki"
  uv build --package lumio-lancedb --wheel --out-dir "$tmp/lance"
  uv venv --python 3.14 "$tmp/base-env"
  uv pip install --python "$tmp/base-env/bin/python" "$tmp"/wiki/lumio_wiki-*.whl
  "$tmp/base-env/bin/python" scripts/verify_lumio_wiki_wheel.py "$tmp"/wiki/lumio_wiki-*.whl tests/fixtures/valid tests/fixtures/categorized_kb
  uv venv --python 3.14 "$tmp/adapter-env"
  uv pip install --python "$tmp/adapter-env/bin/python" "$tmp"/wiki/lumio_wiki-*.whl "$tmp"/lance/lumio_lancedb-*.whl
  "$tmp/adapter-env/bin/python" -c "import importlib.util; assert all(importlib.util.find_spec(n) is None for n in ('stario', 'piccolo', 'openai', 'liteparse', 'markitdown')); import lumio_wiki, sys; assert 'lumio_lancedb' not in sys.modules"
  "$tmp/adapter-env/bin/python" packages/lumio-lancedb/tests/test_lancedb_adapter.py
  ```

  With the pinned disposable SeaweedFS `weed mini` service documented in CI,
  export the live-suite credential aliases and require the onboarding journey
  and all seven provider-neutral modules to execute against the real endpoint:

  ```sh
  export LUMIO_S3_ENDPOINT="${LUMIO_S3_ENDPOINT:-http://localhost:8333}"
  export LUMIO_S3_ALLOW_HTTP="${LUMIO_S3_ALLOW_HTTP:-1}"
  export LUMIO_S3_REGION="${LUMIO_S3_REGION:-us-east-1}"
  export LUMIO_S3_TEST_BUCKET="${LUMIO_S3_TEST_BUCKET:-lumio-release}"
  export LUMIO_S3_ACCESS_KEY_ID="${LUMIO_S3_ACCESS_KEY_ID:-${AWS_ACCESS_KEY_ID:?set AWS_ACCESS_KEY_ID}}"
  export LUMIO_S3_SECRET_ACCESS_KEY="${LUMIO_S3_SECRET_ACCESS_KEY:-${AWS_SECRET_ACCESS_KEY:?set AWS_SECRET_ACCESS_KEY}}"
  uv run pytest -q tests/test_onboarding_journey.py packages/lumio-wiki/tests/test_s3_agent_journey_s3_compat.py packages/lumio-wiki/tests/test_cli_remote_lance_s3_compat.py packages/lumio-wiki/tests/test_artifact_store_s3_compat.py packages/lumio-wiki/tests/test_s3_compat.py packages/lumio-wiki/tests/test_s3_publish_s3_compat.py packages/lumio-lancedb/tests/test_publication_s3_compat.py packages/lumio-lancedb/tests/test_remote_lancedb_s3_compat.py
  ```

  Each module must run rather than skip for a missing endpoint. The optional
  cross-role policy matrix may skip only when separately provisioned role
  credentials are unavailable, and that residual must be recorded.

  **Done:** built artifact versions, independent installs, optional extras, live
  disposable remote journeys and actual consumer commands/revisions are recorded;
  owner separately approves tag/publication/adoption. A passing workspace run or
  skipped S3-compatible module is not release certification. No push/tag/PyPI
  action is authorized by this document or by the current test-deletion lane.
