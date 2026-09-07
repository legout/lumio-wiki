# 05 — Simplification and coordinated release

**Proposed; unexecuted.** Goal: remove obsolete production behavior and duplicate
ownership without removing supported capabilities or blindly breaking external
consumers. Requirements: [B19–B21, C01–C04 and owner API decision](README.md#finding-to-task-map),
[ADR-0022](../adr/0022-retire-activity-log-artifact.md),
[ADR-0025](../adr/0025-repository-split.md),
[Core PRD small seam](../prd/0002-core-sdk.md),
[progressive graph](../adr/0021-entity-claim-ontology-and-progressive-graph-materialization.md).
Python/uv, two public wheels; no mandatory dependencies or new generic engine.

Sequence: inventory real consumers in S2 before S1/S2 exported removals; finish
publication/snapshot/agent correctness first; migrate callers → verify → delete
old paths → search stale references in each cleanup. S3 consumes R1 and the
agreed traversal semantics. **S4 is separately approved release work**, not an
automatic final step. [Global checks](README.md#global-verification-for-future-implementation)
apply at every task boundary.

## S1

- [ ] **Finish Activity Log production retirement, preserve historical safety (B19).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/proposal_pipeline.py`,
  `packages/lumio-wiki/src/lumio_wiki/knowledge_base.py`,
  `packages/lumio-wiki/src/lumio_wiki/records.py`,
  `packages/lumio-wiki/src/lumio_wiki/__init__.py`,
  `packages/lumio-wiki/src/lumio_wiki/s3_publish.py`,
  `CONTEXT.md`, `README.md`, `docs/kb-format.md`;
  `tests/test_kb_control.py`,
  `packages/lumio-wiki/tests/test_entity_merge.py`,
  `packages/lumio-wiki/tests/test_page_removal.py`,
  `packages/lumio-wiki/tests/test_s3_publish.py`.
  **Consumes → produces:** successful reviewed mutations and recognized legacy
  artifacts → navigation-only new publications, no new/append Activity Log writes.
  Prerequisites: P5 and S2's producer-API consumer inventory/migration. Remove
  reachable merge/removal append calls, `append_activity_log_entry`,
  `make_activity_log_entry`, formatting and producer-only records/exports after
  consumers move. Do not delete historical marked logs from existing immutable
  versions; keep loader recognition, canonical fingerprint exclusion and blocking
  unmarked/malformed `log.md` collisions. Ensure new publication assembly does
  not copy a legacy log into new versions. Hot Index and Navigation Index stay
  separate useful features. Update terminology only alongside this runtime change.
  **Obligation:** `existing-check`; current producer tests are intentionally
  retained by plan 01 and change only now. Update
  `test_entity_merge_publishes_one_atomic_candidate` and
  `test_publish_removes_page_regenerates_artifacts_and_logs_activity` to assert
  successful mutation/navigation with no generated/appended log, removing only
  their retired-behavior assertions; rename the latter appropriately. Use the
  current legacy/collision tests and S3 canonical-file journey to assert exclusion.
  **Verify:**

  ```sh
  uv run python -m pytest -q tests/test_kb_control.py packages/lumio-wiki/tests/test_entity_merge.py packages/lumio-wiki/tests/test_page_removal.py packages/lumio-wiki/tests/test_s3_publish.py
  rg -n 'append_activity_log_entry|make_activity_log_entry|_format_activity_log_line' packages tests eval scripts
  ```

  **Done:** no active writer/caller remains (search returns no production matches);
  only historical recognition/collision protections remain, and no old Published
  Version or current navigation capability is removed. Plan/audit references to
  retired symbol names may remain as history.

## S2

- [ ] **Inventory consumers; migrate and narrow duplicated seams (B20/B21/C02/C04).**
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
  **Consumes → produces:** actual in-repo and external consumer import/call
  inventory → a documented primary workflow surface, named secondary module
  owners and a migration ledger with verified consumer revisions/checks.
  Prerequisites: inventory starts first; actual removals follow P1–P5/R1–R5/A3–A6
  interface stabilization. Record private `legout/lumio` and any other confirmed
  consumers supplied by the owner, not hypothetical compatibility users. Private
  app paths/revisions must come from its owner: this public checkout was not
  audited. Without that evidence, exported deletion/release is blocked, not
  silently assumed safe. No private repo writes are authorized by this plan.
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
  **Obligation:** `existing-check`; use load/search/cited retrieval, ingest,
  remote adapter and installed journeys, not resurrected export inventories.
  **Verify inventory, migration and focused behavior:**

  ```sh
  rg -n 'from lumio_wiki|import lumio_wiki|from lumio_lancedb|import lumio_lancedb' packages tests eval scripts examples
  rg -n 'parse_frontmatter|as_sources|graph_path|load_graph_state|DocumentSourceProcessor|cosine_similarity|is_semantic_index_stale|load_lancedb_adapter' packages tests eval scripts examples
  uv run python -m pytest -q tests/test_core_sdk_index.py tests/test_graph_traversal.py packages/lumio-wiki/tests/test_location_snapshot.py packages/lumio-wiki/tests/test_ingest_journey_isolation.py packages/lumio-wiki/tests/test_cli_remote_lance.py packages/lumio-lancedb/tests/test_remote_index_binding.py
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
  **Obligation:** `existing-check`; retain
  `test_legacy_graph_path_still_works`,
  `test_rebuilt_graph_reproduces_public_traversal_results`, graph eligibility
  journeys and ontology parity; migrate legacy caller tests only with the agreed
  semantics, never weaken authorization/bounds for compatibility.
  **Verify:**

  ```sh
  uv run python -m pytest -q tests/test_graph_traversal.py packages/lumio-wiki/tests/test_graph_state.py packages/lumio-wiki/tests/test_graph_retrieval.py packages/lumio-lancedb/tests/test_ontology_parity.py
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
  **Obligation:** `existing-check` for installed wheels/consumer adoption;
  `no-new-test` for release notes. Full retained suite must pass with four workers,
  and independent `[documents]`, `[llm]`, `[all]`, base and S3/adapter tests remain.
  Run from an approved candidate checkout; these commands use disposable venvs:

  ```sh
  set -eu
  uv lock --check
  uv run python -m pytest -q -n 4
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

  With an explicitly provisioned disposable MinIO and the test environment
  documented in CI, also require non-skipped results from:

  ```sh
  uv run python -m pytest -q tests/test_onboarding_journey.py packages/lumio-wiki/tests/test_s3_agent_journey_minio.py packages/lumio-wiki/tests/test_cli_remote_lance_minio.py packages/lumio-wiki/tests/test_artifact_store_minio.py packages/lumio-lancedb/tests/test_publication_minio.py packages/lumio-lancedb/tests/test_remote_lancedb_minio.py
  ```

  **Done:** built artifact versions, independent installs, optional extras, live
  disposable remote journeys and actual consumer commands/revisions are recorded;
  owner separately approves tag/publication/adoption. A passing workspace run or
  skipped MinIO is not release certification. No push/tag/PyPI action is authorized
  by this document or by the current test-deletion lane.
