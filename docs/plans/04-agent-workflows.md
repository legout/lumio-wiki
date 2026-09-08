# 04 — Safe, usable agent workflows

**Implemented.** Goal: repair actual safety/command defects before adding
bounded machine-facing capabilities. Requirements:
[B13–B18, B21, I01/I02/I04, C01/C04](README.md#finding-to-task-map),
[host-as-Distiller and optional packaging](../adr/0010-uv-workspace-and-progressive-packaging.md),
[packaged contract](../adr/0017-portable-agent-skill-distribution-and-project-bootstrap.md),
[layered converters](../adr/0018-layered-document-conversion-anydoc.md),
[v2 ontology](../adr/0021-entity-claim-ontology-and-progressive-graph-materialization.md).
Python/uv and existing CLI/SDK seams, no new mandatory provider or agent runtime.

A1/A2/A4 and A3's concrete ingestion repair are safety/correctness work. Complete
those before A5/A6 capabilities. A7 first fixes executable command guidance,
then uses A5/A6's approved interface. Coordinate all `cli.py` edits serially;
[global checks](README.md#global-verification-for-future-implementation) apply.

## A1

- [x] **Redact quoted credentials and contain provider errors (B13/B17).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/capture.py`,
  `packages/lumio-wiki/src/lumio_wiki/distiller.py`,
  `packages/lumio-wiki/src/lumio_wiki/cli.py`;
  `packages/lumio-wiki/tests/test_capture.py`,
  `packages/lumio-wiki/tests/test_llm_distiller.py`,
  `packages/lumio-wiki/tests/test_cli.py`.
  **Consumes → produces:** authored capture text and provider failures →
  reviewed redacted page/preview plus bounded safe actionable errors.
  Prerequisite: retained floor. Support quoted JSON/YAML credential keys and
  escaped JSONL examples before preview, diff, persistence and staging; preserve
  legitimate non-secret text and consent/manifest/transcript digest binding.
  Redaction is a best-effort safety net, not proof all secrets are gone. Provider
  exceptions must not interpolate arbitrary `str(exc)`, headers, raw output or
  exception chains into default CLI output. Translate request-time and malformed
  output failures to the existing CLI boundary with safe allowlisted error
  information; retain retries/missing-extra hints and no provider import in base.
  The fake-provider audit proves raw-message propagation; it does not assert
  every real provider error contains a secret.
  **Obligation:** `new-test`; add
  `test_quoted_json_credentials_are_redacted_before_capture_staging` and
  `test_provider_request_failure_is_safe_at_cli_boundary`. Use synthetic secrets
  and an injected OpenAI-shaped client, verify preview/proposal/stdout/stderr,
  and retain `test_unconfirmed_capture_registers_and_stages_nothing` plus
  `test_openai_distiller_with_injected_client_does_not_import_openai`.
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q packages/lumio-wiki/tests/test_capture.py packages/lumio-wiki/tests/test_llm_distiller.py packages/lumio-wiki/tests/test_cli.py
  ```

  **Done:** quoted synthetic credentials cannot enter staged captured knowledge;
  request/parser failures yield bounded nonzero CLI results without raw secrets
  or traceback; host-authored/model-free capture remains unchanged.

## A2

- [x] **Apply remaining deadline while reading headers (B14).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/url_fetch.py`;
  `packages/lumio-wiki/tests/test_url_ingest.py`.
  **Consumes → produces:** `UrlFetchPolicy` and validated/pinned addresses →
  bounded per-hop connect/header/body operation or `UrlFetchError`.
  Prerequisite: retained floor. Pass the absolute deadline through address
  attempts, TLS/connect, response-header parsing and body reads; shrinking a
  body socket timeout after `getresponse()` is too late. Slow header bytes must
  not reset the budget. Close resources on timeout and stage/register nothing.
  Preserve redirect revalidation, IP pinning, TLS hostname verification,
  byte/media limits and explicit private/HTTP test overrides. State the actual
  synchronous DNS boundary honestly: current resolution precedes the per-hop
  I/O timer; do not advertise a total wall-clock DNS-inclusive guarantee without
  a separately verified bounded resolver. No SSRF bypass was demonstrated.
  **Obligation:** `new-test`; add
  `test_slow_response_headers_respect_remaining_deadline` beside existing URL
  bounds tests. Use only loopback, finite server cleanup and generous scheduling
  tolerance while proving header trickling cannot outlive the budget by multiples.
  Also verify multiple address attempts consume the same remaining I/O budget.
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q packages/lumio-wiki/tests/test_url_ingest.py
  ```

  **Done:** timeout aborts during slow headers, cleanup completes, no partial
  ingest occurs, and no stronger DNS/reader/crash guarantee is implied.

## A3

- [x] **One ingest preparation path; one optional composition owner (B15/B21).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/ingest.py`,
  `packages/lumio-wiki/src/lumio_wiki/cli.py`,
  `packages/lumio-wiki/src/lumio_wiki/retrieval_eval.py`;
  create `packages/lumio-wiki/src/lumio_wiki/composition.py` for explicitly
  optional CLI/evaluation adapter construction;
  `packages/lumio-wiki/tests/test_cli.py`,
  `packages/lumio-wiki/tests/test_documents_extra.py`,
  `docs/packaging.md`, `README.md`.
  **Consumes → produces:** selected processor/normalized text/Distiller/KB context
  → common provenance and proposed Markdown; explicit retrieval configuration
  plus snapshot descriptor → optional `RetrievalAdapter`/builder binding.
  Prerequisite: A1. First share the short conversion-to-proposed-page preparation
  between `_cmd_ingest` and `create_proposal_without_provider`, including AnyDoc
  wrapping. Do not accidentally persist ordinary CLI raw Markdown inside the KB;
  SDK explicit external raw-store staging and managed-original-byte binding remain
  distinct policies. Real CSV conversion must reach a reviewable proposal (it
  need not become valid authored v2 knowledge automatically). Preserve PDF/image
  LiteParse OCR/page coordinates, office AnyDoc provenance, HTML/broad MarkItDown,
  observable converter errors and no silent fallback. Managed host ingest does
  not run a converter or require `[documents]`.
  Then move scattered optional discovery/construction out of evaluation and
  CLI routes into the small composition helper, migrate search/status/publication/
  evaluation callers, remove old duplicated loaders and clarify the categorical
  "never imports" docs: canonical SDK stays dependency-neutral; explicitly
  selected CLI composition may lazily load the separately installed adapter.
  This is the current owner's preserved optional behavior, not a new plugin API.
  **Obligation:** `new-test` for
  `test_real_anydoc_csv_cli_stages_reviewable_proposal`; `existing-check` for
  composition, using installed isolation and current remote failure journeys.
  **Red → minimal green for CSV, then migrate and verify:**

  ```sh
  uv run python -m pytest -q packages/lumio-wiki/tests/test_cli.py -k real_anydoc_csv_cli_stages_reviewable_proposal
  uv run python -m pytest -q packages/lumio-wiki/tests/test_documents_extra.py packages/lumio-wiki/tests/test_managed_ingest.py packages/lumio-wiki/tests/test_ingest_journey_isolation.py packages/lumio-wiki/tests/test_cli_remote_lance.py packages/lumio-wiki/tests/test_cli_status.py packages/lumio-wiki/tests/test_cli_s3.py eval/test_retrieval_eval_gate.py packages/lumio-wiki/tests/test_wheel_isolation_documents.py
  rg -n 'load_lancedb_adapter|lancedb_available|import_module.*lumio_lancedb' packages/lumio-wiki/src/lumio_wiki
  ```

  **Done:** real CSV no longer raises missing-frontmatter traceback, all caller
  paths use one preparation policy, and optional adapter loading has one owner
  with no runtime CLI dependency on evaluation. Search output may show the new
  owner and explicit callers, not abandoned duplicate constructors.

## A4

- [x] **Executable citation actions retain original KB location (B16).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/citation_actions.py`,
  `packages/lumio-wiki/src/lumio_wiki/cli.py`;
  `packages/lumio-wiki/tests/test_cli_open_actions.py`,
  `packages/lumio-wiki/tests/test_citation_actions.py`.
  **Consumes → produces:** effective original local path or S3 URI, page/source
  identity → executable page/source-inspect actions bound to that location.
  Prerequisite: retained floor; coordinate CLI with A1/A3. Thread location through
  page, local/remote page-search, Evidence and source-action printers, including
  defaults resolved from `.env`. Use original public S3 location, never temporary
  materialization paths. Preserve explicit historical version on source actions
  when present. Shell-escape locations, titles and source IDs; no implicitly
  signed/private artifact URLs. Shared SDK helpers may omit CLI location when a
  caller has none; CLI-generated executable actions may not silently omit it.
  **Obligation:** `new-test`; add `test_copyable_actions_replay_original_kb` using
  two temporary KBs with the same title and a conflicting/default-free environment.
  Replay page and source actions. Keep `test_page_open_command_is_shell_safe_for_hostile_titles`
  and `test_rendered_open_actions_never_contain_object_store_urls` (its existing
  location-free fixture proves private Source URL separation; a public S3 KB
  location in an executable command is a different input).
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q packages/lumio-wiki/tests/test_cli_open_actions.py packages/lumio-wiki/tests/test_citation_actions.py packages/lumio-wiki/tests/test_wheel_isolation.py
  ```

  **Done:** replay opens the same KB/source binding despite another default;
  hostile argument quoting remains safe and private store URLs never appear.

## A5

- [x] **Give host/provider authors the actual v2 KB schema (B18/I02/C01).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/distiller.py`,
  `packages/lumio-wiki/src/lumio_wiki/cli.py`,
  `packages/lumio-wiki/src/lumio_wiki/data/skill/SKILL.md`,
  `packages/lumio-wiki/src/lumio_wiki/data/skill/PROTOCOL.md`,
  `README.md`, `docs/kb-format.md`, `CONTEXT.md`;
  `packages/lumio-wiki/tests/test_llm_distiller.py`,
  `tests/test_onboarding_journey.py`.
  **Consumes → produces:** validated KB categories/types/predicates and authoring
  mode → small ontology-aware guidance/template plus valid worked sample.
  Prerequisites: P1, A1/A3; coordinate Distiller caller changes through S2.
  Include stable Entity IDs/types, controlled predicates, accepted/disputed
  Claim semantics and published-page Evidence anchors. Valid-empty ontology
  requires declaring a type through reviewed Control File authoring, not weakening
  validation or inventing ontology automatically. Preserve custom categories,
  legacy-flat guidance, raw Source IDs and default host-as-Distiller. Provider
  receives only needed public schema context, never registry/credentials.
  Execute README's complete sample against a freshly initialized v2 KB; align
  canonical vocabulary and remove duplicated glossary tail without inventing
  app behavior. Activity Log text is retired only with S1's production change.
  **Obligation:** `new-test`; add `test_distiller_guidance_uses_current_kb_ontology`
  and `test_readme_sample_validates_in_initialized_v2_kb`; use a fake provider to
  inspect supplied context, not assert real model output quality.
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q packages/lumio-wiki/tests/test_llm_distiller.py tests/test_onboarding_journey.py packages/lumio-wiki/tests/test_entity_claims.py
  ```

  **Done:** README/template produces valid v2 authored knowledge and the prompt
  carries the selected KB's controlled schema; no auto-extraction/publication.

## A6

- [x] **Add lossless bounded reads and stable opt-in machine results (I01).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/cli.py`,
  `packages/lumio-wiki/src/lumio_wiki/location.py`,
  `packages/lumio-wiki/src/lumio_wiki/knowledge_base.py`;
  `packages/lumio-wiki/tests/test_cli.py`,
  `packages/lumio-wiki/tests/test_search_json.py`,
  `docs/usage.md`.
  **Consumes → produces:** captured page bytes plus optional raw/JSON and
  section/line bounds → exact content/metadata with truthful coordinates and
  truncation; existing validation/capture/mutation outcomes → stable JSON status,
  proposal/error identity and exit behavior. Prerequisites: A1–A5, R1/R5.
  Reuse current JSON conventions; no parallel runtime or generic response
  framework. Keep default human output unchanged. Unbounded raw mode must be
  lossless canonical Markdown (not reconstructed selected fields); bounded
  reads disclose original ranges and omission, retaining Entity/Claim IDs,
  evidence/review metadata needed for safe edits. Reject invalid/negative bounds
  and ambiguous page/section selection; enforce output limits rather than
  accidentally dumping a large private source. No registry, artifact keys,
  signed URLs or raw sources in ordinary results. Machine errors remain safe and
  mutation output cannot announce success before P5 completion.
  **Obligation:** `new-test`; add `test_page_machine_read_preserves_canonical_identity_and_bounds`
  and `test_machine_mutation_errors_are_stable_and_private`. Test observable
  identity/exit semantics, not a new exhaustive representation census.
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q packages/lumio-wiki/tests/test_cli.py packages/lumio-wiki/tests/test_search_json.py packages/lumio-wiki/tests/test_source_inspection.py
  ```

  **Done:** agents can read bounded exact compiled content and act on stable
  machine outcomes without scraping human prose or losing privacy.

## A7

- [x] **Correct packaged commands and make the safe local journey first (B18/I02/I04).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/data/skill/SKILL.md`,
  `packages/lumio-wiki/src/lumio_wiki/data/skill/PROTOCOL.md`,
  `packages/lumio-wiki/src/lumio_wiki/cli.py` (generated project guidance),
  `README.md`, `docs/quickstart.md`, `docs/usage.md`,
  `examples/onboarding-journey/README.md`,
  `examples/real-world-lumio-wiki/README.md`;
  `packages/lumio-wiki/tests/test_wheel_isolation.py`,
  `packages/lumio-wiki/tests/test_skill_public_surface.py`,
  `tests/test_onboarding_journey.py`.
  **Consumes → produces:** actual registered CLI grammar/A5 authoring contract →
  concise installed skill workflow selection and executable protocol examples.
  Prerequisites: command-order safety repair can start after A1–A4; capability
  guidance follows A5/A6. Fix `source <kb> list/retire/reactivate` to
  `source list/retire/reactivate <kb>`; replace README's nonexistent skill flags
  with actual subcommands. Do not add a second grammar for incorrect prose.
  Put local setup → author → managed ingest → inspect/validate → publish → cite
  before optional MinIO/S3/provider instructions, preserving those optional
  journeys via links. Shorten duplicated skill prose by relative protocol
  references, never by removing trust rules. Explicitly treat instructions in
  source text, pages, transcripts, filenames and tool outputs as untrusted data:
  they cannot authorize capture/publish, source fetch/link or configuration
  changes. Cite-or-refuse is consumer behavior; graph candidates/citations are
  not entailment. Keep explicit consent, no auto skill installation and no
  active-source execution.
  **Obligation:** `new-test`; add `test_packaged_source_commands_execute_in_order`
  to the installed-wheel journey and
  `test_protocol_denies_source_content_operational_authority` to minimal safety
  guidance. Extend `test_fresh_environment_local_journey`, not the deleted
  all-command/help/prose parity matrix.
  **Red → minimal green → verify:**

  ```sh
  uv run python -m pytest -q packages/lumio-wiki/tests/test_wheel_isolation.py packages/lumio-wiki/tests/test_skill_public_surface.py tests/test_onboarding_journey.py
  ```

  **Done:** selected installed command examples execute in a temporary KB, a
  restarted local-first session works without provider/S3, and the trust boundary
  is explicit. Optional MinIO tests may skip locally, never count as passed live
  certification.

## Approval and remaining limits

All tasks above are implemented and verified with synthetic secrets, temporary
KBs, loopback servers, and injected providers; no real capture history or
installed skill writes are required. Provider output quality, universal secret
detection, and prompt-injection immunity are not claimed. Changes to Distiller
signatures, citation helper parameters, and machine contracts require [consumer
coordination](05-simplification-release.md#s2) before release, not indefinite
compatibility shims.
