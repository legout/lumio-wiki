# 03 — Captured snapshots and useful retrieval

**Proposed; unexecuted.** Goal: every derived result identifies the bytes it
actually used, and passage retrieval spends its budget on useful Evidence.
Requirements: [B08–B12, I03, C03](README.md#finding-to-task-map),
[Core PRD](../prd/0002-core-sdk.md),
[S3 snapshots](../adr/0013-s3-native-knowledge-base-locations.md),
[ontology/graph](../adr/0021-entity-claim-ontology-and-progressive-graph-materialization.md).
Use Python/uv, existing source abstraction, MessagePack, optional LanceDB and
in-memory obstore tests. No new storage framework or mandatory embedder.

R1 is the common identity prerequisite; R2/R3 consume it, R4 consumes historical
R3-compatible snapshots, R5 shares parser coordinates with P1, and R6 measures
R1/R2/R5 behavior. Integrate shared files serially. [Global validation](README.md#global-verification-for-future-implementation)
always follows the exact focused commands below.

## R1

- [ ] **Capture once; bind pages/index/graph to the captured digest (B08).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/knowledge_base.py`,
  `packages/lumio-wiki/src/lumio_wiki/location.py`,
  `packages/lumio-wiki/src/lumio_wiki/graph_state.py`;
  `tests/test_zero_index_retrieval.py`,
  `packages/lumio-wiki/tests/test_location_snapshot.py`,
  `packages/lumio-wiki/tests/test_graph_state.py`.
  **Consumes → produces:** `_KbSource` canonical path/byte capture → loaded
  pages/control, validation, fingerprint and graph facts with one immutable
  content identity. Prerequisite: P1 and retained floor. Reuse `_InMemoryKbSource`
  and canonical classification rather than a second storage abstraction. Parse,
  validate and hash the same captured bytes; loading then rehashing live disk
  does not establish this guarantee. `build_index`, graph materialization,
  fallback/graph-triggered rebuild, returned KB copies and `FilesystemLocation`
  snapshots must retain that identity. A caller-created record-only KB must not
  claim live-disk freshness without a verified capture. Detect filesystem drift
  when advertising current-source builds: explicitly reload or reject the stale
  view, never stamp old pages with a new disk digest. Old snapshot reads may
  remain valid immutable views without claiming they reflect current disk.
  **Obligation:** `new-test`; add `test_loaded_view_cannot_stamp_old_pages_fresh`
  to `test_zero_index_retrieval.py`, extending it through graph materialization
  and graph-triggered rebuild. Keep `test_kb_zero_index_build_index_roundtrip_freshness`
  and snapshot/graph corruption journeys.
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q tests/test_zero_index_retrieval.py packages/lumio-wiki/tests/test_location_snapshot.py packages/lumio-wiki/tests/test_graph_state.py
  ```

  **Done:** an intervening disk edit never yields old Evidence/graph stamped
  fresh against newer content; all captured derivatives share one digest.

## R2

- [ ] **Invalidate semantic state on lexical-only rebuild (B09).**
  **Files:** `packages/lumio-lancedb/src/lumio_lancedb/index.py`,
  `packages/lumio-lancedb/src/lumio_lancedb/location.py`;
  `packages/lumio-lancedb/tests/test_remote_index_location.py`.
  **Consumes → produces:** captured pages/fingerprint and optional model →
  consistent lexical tables plus either matching semantic state or explicit
  unavailable semantic state. Prerequisite: R1. Prefer invalidating the old
  `evidence_vectors` table and embedding-model metadata on lexical rebuild over
  preserving a second lifecycle. Semantic/hybrid requests must demand rebuilding
  or use the existing truthful eligible-page fallback policy, never old text or
  deleted paths. Commit freshness/completion only after all requested tables
  succeed; failed semantic builds cannot bless partially new/old tables.
  Preserve model/dimension validation, remote immutability and no private-source
  data in tables. Use the existing location seam for local/remote metadata.
  **Obligation:** `new-test`; add
  `test_lexical_rebuild_invalidates_obsolete_semantic_evidence` and
  `test_failed_semantic_rebuild_does_not_stamp_index_fresh`; the first walks
  semantic build → edit/delete → reload → lexical build → semantic/hybrid query
  with the same deterministic embedder. This proves lifecycle, not model quality.
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q packages/lumio-lancedb/tests/test_remote_index_location.py packages/lumio-lancedb/tests/test_remote_index_binding.py packages/lumio-lancedb/tests/test_graph_tables.py
  ```

  **Done:** no stale vectors/model metadata are accepted under a newer lexical
  fingerprint, including failed builds and eligibility-constrained fallback.

## R3

- [ ] **Publish S3 content and private bindings from one candidate (B10).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/s3_publish.py`,
  `packages/lumio-wiki/src/lumio_wiki/artifact_store.py`,
  `packages/lumio-wiki/src/lumio_wiki/cli.py`,
  `packages/lumio-lancedb/src/lumio_lancedb/publish.py`;
  `packages/lumio-wiki/tests/test_s3_publish.py`,
  `packages/lumio-wiki/tests/test_artifact_store.py`.
  **Consumes → produces:** R1 captured canonical candidate plus P3-consistent
  private source-version bindings → immutable canonical bytes, digest, graph,
  requested index, public completion manifest and separate private binding
  manifest for the **same** pages. Prerequisites: R1, P3 and R2 for enhanced
  builds. Thread the captured candidate through `PreparedVersion`/builder/hook
  composition; the artifact hook must not reload `source_root`. Resolve/capture
  referenced registry Source Versions consistently under the shared mutation
  ownership, not later registry-current values. Keep private bindings out of
  public manifests and errors. Capture remote pointer CAS intent before costly
  preparation, reject reused version prefixes, require requested index health
  and binding coverage, then activate once. Optional retention may omit
  unregistered sources; required retention cannot. A local edit during build may
  affect a later publish, never this candidate's identity.
  **Obligation:** `new-test`; add
  `test_s3_publish_uses_one_captured_candidate` (edit at the old fingerprint/read
  window) and `test_activation_bindings_use_captured_pages` (edit during builder
  callback), with actual MemoryStore and binding hook, no live KB/network.
  Preserve `test_activation_cas_uses_the_originally_observed_pointer` and
  `test_publication_writes_private_binding_manifest_before_activation`.
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q packages/lumio-wiki/tests/test_s3_publish.py packages/lumio-wiki/tests/test_artifact_store.py packages/lumio-wiki/tests/test_cli_s3.py
  ```

  **Done:** activated versions always resolve, private page/source sets equal
  the captured public candidate, and concurrent activation still loses by CAS.

## R4

- [ ] **Confirm partial historical binding holes, then fail closed (B11).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/artifact_store.py`,
  `packages/lumio-wiki/src/lumio_wiki/s3_publish.py`,
  `packages/lumio-wiki/src/lumio_wiki/cli.py`;
  `packages/lumio-wiki/tests/test_artifact_store.py`.
  **Consumes → produces:** resolved target Published Version's immutable page
  set/fingerprint and private historical manifest → required-retention decision
  before rollback CAS. Prerequisite: R3. The audit reports a real empty-manifest
  repro; **confirm it at the implementation base before changing code**. Publish
  an unregistered non-synthetic source with optional retention, then request
  required rollback through the real hook/CLI. Missing-manifest tests alone do
  not reproduce the legitimate partial-manifest case. Check every historical
  required `(page, source_id)` pair, not merely existing entries; verify manifest
  version/fingerprint and each bound artifact's digest/size. Empty manifests are
  valid only for an actually empty required set. Never substitute current
  registry versions for missing historical bindings. Required policy fails
  closed for incomplete history; explicitly optional policy stays optional.
  **Obligation:** `new-test`; add
  `test_required_rollback_rejects_partial_historical_binding_manifest` alongside
  `test_verify_rollback_coverage_gates_historical_activation`. If the alleged
  hole cannot be reproduced, record contrary evidence and review this task
  rather than implementing a speculative fix.
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q packages/lumio-wiki/tests/test_artifact_store.py packages/lumio-wiki/tests/test_source_inspection.py packages/lumio-wiki/tests/test_cli_s3.py
  ```

  **Done:** required rollback certifies the target snapshot's complete required
  set before CAS; historical inspection remains exact and optional mode works.

## R5

- [ ] **Emit nested passages without duplicate whole-page budget use (B12).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/evidence.py`,
  `packages/lumio-wiki/src/lumio_wiki/retrieval.py`,
  `packages/lumio-wiki/src/lumio_wiki/knowledge_base.py`,
  `packages/lumio-lancedb/src/lumio_lancedb/index.py`;
  `tests/test_zero_index_retrieval.py`,
  `packages/lumio-lancedb/tests/test_remote_index_location.py`.
  **Consumes → produces:** captured Markdown and query → Evidence with focused
  section identity, exact file line ranges/text and meaningful bounded snippets.
  Prerequisites: R1/P1; R2 for consistent adapter rebuilds. Visit H1/H2/H3 children
  rather than jumping over the whole parent. Share heading recognition with
  Claim anchor validation (currently a different H1–H6 regex). Preserve content
  before headings and useful parent context, but deduplicate equivalent spans or
  prefer the specific matching passage before applying the result limit. Handle
  long unheaded content with a query-relevant snippet, without inventing citation
  coordinates or changing raw source privacy. No generic chunking framework.
  **Obligation:** `new-test`; add `test_nested_section_retrieval_cites_specific_passage`
  at the zero-index seam and `test_lancedb_nested_passage_preserves_citation`
  at the adapter seam. Use the audit's late `Limits` fact after a long introduction;
  assert exact range/text, matching snippet and nonduplicated limited results.
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q tests/test_zero_index_retrieval.py packages/lumio-lancedb/tests/test_remote_index_location.py packages/lumio-wiki/tests/test_entity_claims.py
  ```

  **Done:** both adapters return the specific nested passage within budget;
  Claim anchors and retrieved citations agree about headings/coordinates.

## R6

- [ ] **Measure negatives, passages and mutation/index lifecycle (I03).**
  **Files:** `eval/gold_set.yaml`,
  `examples/real-world-lumio-wiki/evaluation/gold-v1.yaml`,
  `eval/test_retrieval_eval_gate.py`,
  `packages/lumio-wiki/src/lumio_wiki/retrieval_eval.py`,
  `packages/lumio-wiki/tests/test_retrieval_eval.py`.
  **Consumes → produces:** current gold queries plus explicit empty-relevance
  queries/passage expectations → disclosed negative-result, citation and
  lifecycle measurements alongside recall. Prerequisites: R1/R2/R5; reuse A3
  optional composition. Add natural-language hard negatives sharing common
  terms, not only a missing token. Exercise accepted/disputed/superseded and
  source edit/delete/rebuild transitions using the regressions above, not a new
  cross-product suite. Empty relevance needs a defined negative metric, not
  misleading recall division. Keep candidates distinct from answer support:
  finding common words is not a cite-or-refuse violation in the SDK. A stopword
  or significant-term ranking change is a **hypothesis**, only retained if
  measured recall/negative behavior improves without violating the essential
  gates. Do not claim a fixed-hash synonym embedder proves real semantic ranking.
  **Obligation:** `new-test`; add
  `test_hard_negative_and_passage_metrics_are_disclosed`; preserve
  `test_ac3_graph_expansion_measurably_lifts_recall`,
  `test_lancedb_stages_available_and_measured`, and
  `test_lancedb_semantic_catches_synonym_paraphrase` as wiring checks.
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q eval/test_retrieval_eval_gate.py packages/lumio-wiki/tests/test_retrieval_eval.py eval/test_ontology_eval_gate.py
  ```

  **Done:** results disclose corpus, available/skipped stages, model identity and
  what was not measured; hard-negative/passage regressions distinguish failure.
  Real provider/model ranking certification requires a separately approved,
  reproducible dataset/model run, not an automatic network call in base CI.

## Retained risks

Capture can be internally consistent without representing an atomic instant of
an externally edited filesystem: validate the captured candidate and document
that ceiling. Multi-process cooperating capture/writes use P3; remote activation
still needs CAS. Derived state is disposable, not canonical. MinIO gates remain
necessary for actual remote operation; MemoryStore race tests do not certify AWS
permissions, live LanceDB storage or real embedding quality.
