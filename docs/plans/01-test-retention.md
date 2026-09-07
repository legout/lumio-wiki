# 01 — Implemented test retention and reduction

**Implemented bounded reduction only; no production fixes.** Requirements:
[owner essential-journey policy](README.md#owner-decisions-and-authority) and the
two complete final essential-journey reports (core/workflows), identified in
[the evidence registry](README.md#owner-decisions-and-authority). Earlier
opposite golden-versus-semantic selections were not combined with these maps.

## T1 — Applied reduction and validation

- [x] **Delete only the approved map; keep survivor behavior.** Files: the exact
  inventory below under `tests/`, `packages/*/tests/`, `eval/test_*.py`, plus the
  three orphan exchange goldens. Input → output: pinned suite/map → smaller
  essential suite with unchanged runtime and unchanged surviving test bodies.
  Prerequisite: pinned clean base `67b45ab4ac114d674ec900c7f17b171eae5e92fa`.
  **Obligation:** `existing-check`; this is actual deletion, no replacement tests,
  hidden loops, parametrization changes, skip changes or collection config edits.
  Read all selected bodies; cross-check both maps' named dependencies; remove
  only newly orphaned scaffolding/imports and stale deleted-section commentary.
  **Completion:** exact collection delta and unchanged survivor ASTs verified;
  focused serial and full four-worker runs pass, Ruff and diff checks clean.
- [x] **Document the reduction and unexecuted plans.** Files: `docs/plans/README.md`
  and plans 01–05. Input → output: approved decisions/current audit/code → linked
  task map. **Obligation:** `no-new-test`; source paths, local links, task anchors,
  finding coverage and absence of unresolved placeholders checked. This is not
  authorization to implement plans 02–05 or a claim their defects are fixed.

| Measurement | Exact result |
| --- | ---: |
| Removed test functions | 211 |
| Removed collected node IDs (including parameter expansion) | 225 |
| Collection before → after | 1,713 → 1,488 |
| Retained test-function ASTs compared unchanged | 1,360 |
| Explicitly named survivor definitions cross-checked across both reports | 140 |
| Test-source files touched (two deleted completely) | 37 |
| Orphan golden files removed | 3 |
| Test/fixture diff added / deleted / net removed lines | 24 / 3951 / 3927 |

The source maps cover 2,574 test/helper lines; the goldens are **745 logical
lines**, not the reports' 744 (GraphML has 272). Applied diff counts additionally
include attached blank separators, newly orphaned helpers/imports and removed
empty section comments; a few module summaries now describe retained coverage.
No count target justified any additional test removal. Five extra newly orphaned
items were source/reference checked: parity `corpus_dir`, CLI `categorized_kb`,
S3 routing `_StubS3Location`/`stub_s3_resolution`, capture-adapter `FIXTURES`.
Keep still-used `FORMAT_PROVENANCE`, onboarding `_collapse`, capture-adapter
`ROOT`, wheel fixtures and safety helpers. Pre-existing unrelated unused helpers
were not cleaned up. No map conflict required retaining a selected function.

## Essential survivors and accepted loss

Paths below abbreviate **W** = `packages/lumio-wiki/tests/`, **L** =
`packages/lumio-lancedb/tests/`. All **unlisted** tests remain, not just these
representative named journeys. No Activity Log writer test was removed: S1 is
still unimplemented. A removed test with a safety-sounding name was inspected;
for example its alleged secret-trigger strings never reached production, while
retained unknown-action/status tests actually exercise hostile inputs.

| Capability | Essential named survivors | Accepted coverage loss |
| --- | --- | --- |
| Base SDK, freshness, cited retrieval | `tests/test_core_sdk_index.py::test_rebuilding_restores_freshness`, `test_page_search_matches_all_supported_fields`; `tests/test_zero_index_retrieval.py::test_zero_index_adapter_retrieve_returns_cited_results`, `test_zero_index_adapter_unknown_and_limit_and_semantic` | Composite smoke, private app aliases, exact type/export inventories and implementation construction counts |
| Navigation/Hot Index | `tests/test_kb_control.py::test_publish_reserved_artifacts_generates_nav_indexes_and_hot_index`, `test_regenerate_reserved_artifacts_regenerates_hot_index_without_churn`, `test_valid_hot_index_does_not_change_canonical_fingerprint` | Duplicate CLI label/subdirectory/no-pin samples, not artifact generation |
| Ontology/entity identity | `W/test_entity_claims.py::test_minimal_entity_kb_loads_successfully`, `test_unknown_predicate_is_blocked`, `test_subject_domain_violation_is_blocked`, `test_unknown_evidence_section_is_blocked`, `test_redirect_cycle_is_blocked`; `W/test_entity_resolution.py::test_resolve_entity_by_exact_id_returns_one_stable_entity` | Export census, record shape, repeated resolution and duplicated core-only adapter validation |
| Graph topology, scope and bounds | `W/test_graph_structural_diagnostics.py::test_canonical_counts_and_directionality`, `test_weakly_connected_components_and_coverage`; `W/test_graph_retrieval.py::test_graph_expansion_restricts_results_to_eligible_pages`, `test_trace_does_not_fabricate_graph_stage_when_unused`; `tests/test_graph_traversal.py::test_legacy_graph_path_still_works` | Diagnostic tuple/frozen shape, exact tie ordering, empty/self-loop/parallel diagnostic examples and trace detail strings; traversal parallel-edge budgets stay |
| Reference parsing and cache integrity | `W/test_discovery_graph.py::test_nested_relative_link_resolves_via_source_directory`; `W/test_graph_state.py::test_artifact_contains_incoming_and_outgoing_adjacency`, `test_atomic_write_does_not_clobber_on_simulated_failure`, `test_graph_materialization_does_not_require_lancedb_or_pyarrow` | Minor link syntax examples, repeated equality, positive metadata probes; corruption, stale versions, auth, rollback and bounds stay |
| JSON/GraphML exchange | `W/test_graph_exchange.py::test_export_graph_node_link_shape_over_eval_fixture`, `test_export_graph_graphml_matches_json`, `test_round_trip_export_import_stages_one_clean_proposal` | Byte-identical JSON/XML and stub Markdown goldens; visibility, source/body exclusion, malformed/colliding imports and both CLI journeys stay |
| Enhanced retrieval/evaluation | `L/test_graph_tables.py::test_load_graph_state_matches_zero_index_graph`, `test_search_entity_candidates_returns_scored_review_candidates`; `eval/test_retrieval_eval_gate.py::test_lancedb_stages_available_and_measured`, `test_lancedb_semantic_catches_synonym_paraphrase`, `test_ac3_graph_expansion_measurably_lifts_recall` | Direct LanceDB query probes, stand-in vector math/report representation and exhaustive disclosure keys; these tests prove wiring, not real ranking quality |
| Installed base and extras | `W/test_wheel_isolation.py::test_isolated_init_ingest_publish_journey`, `test_isolated_retrieval_ladder`, `test_isolated_search_page_related_paths`; all existing documents/LLM/all/S3 extra journeys | Two duplicated heavyweight/OpenAI absence probes; the ladder still checks all forbidden imports. No wheel build/venv fixture removed |
| Configuration and local onboarding | `W/test_cli.py::test_subprocess_journey_reads_env_for_path_commands`, `test_subprocess_env_file_does_not_leak_arbitrary_keys`; `tests/test_onboarding_journey.py::test_fresh_environment_local_journey` | Argparse/help/default labels, source-tree command census, duplicate in-process env precedence and exact prose parity |
| S3 reader/publication | `W/test_cli_setup_s3.py::test_fresh_process_exported_publish_to_beats_env_file`; `W/test_cli_remote_lance_minio.py::test_cli_status_reader_journey_with_lancedb`; all immutable publish/CAS/failure/fallback checks | Mocked healthy-status/config representation and positive command-routing microchecks. Some healthy remote positives now depend primarily on **optional MinIO** and are not certified by this default run |
| Managed ingest and capture | `W/test_ingest_journey.py::test_plain_proposal_validates_against_existing_pages`; `W/test_managed_ingest.py::test_managed_ingest_publish_never_leaks_raw_bytes_or_registry_state`; `W/test_capture.py::test_confirmed_capture_stages_single_reviewable_proposal`, `test_unconfirmed_capture_registers_and_stages_nothing`; `W/test_capture_adapters.py::test_export_then_preview_composes_through_capture_contract` | Per-format provenance cross-product, five-client declarative manifest matrix, exact discovery ordering/help. Dedicated generic Claude Code/Hermes fixture successes are lost; clients/formats still supported. Consent, raw transcript bounds, exact bytes/digests and redaction stay |
| Skill safety and privacy | `W/test_skill_management.py::test_failed_atomic_update_restores_previous_bundle`, `test_concurrent_fallback_updates_are_serialized`; `W/test_wheel_isolation.py::test_isolated_skill_upgrade_from_prior_wheel`; `W/test_skill_public_surface.py::test_source_artifacts_documented_optional_private_and_not_evidence` | Exact vendor destination/manifest matrix and broad source-tree phrase parity. Installed essential guidance, explicit install, symlink/corruption/recovery and secrecy stay |
| Private source identity/lifecycle | `W/test_cli.py::test_source_resolve_by_exact_source_id_emits_json`, `test_source_fetch_writes_byte_exact_original`; `W/test_source_lifecycle.py::test_proposal_source_lifecycle_metadata_round_trips_without_page_changes`, `test_private_registry_activity_does_not_change_kb_or_export_bytes`; `W/test_source_resolution.py::test_resolve_ambiguity_is_bounded_with_truthful_truncation_note` | Direct positive ID/title/path examples, private directory representation, exports and safe-label literals. Ambiguity, no-guess, historical binding, digest/size, destination sanitation, retirement/rollback and malicious-input checks stay |
| Review-after | `W/test_review_after.py::test_status_counts_due_pages`, `test_status_zero_due_without_field` | Exact plaintext status label; advisory dates, validation and maintenance reporting stay |

The selected generated-AGENTS S3 microcheck contained a private-URI exclusion
assertion. Its always-running setup journey still checks the same actual
`AGENTS.md` output excludes that private URI; it was not treated as permission
to disclose private stores. ADR-0017/0024's older exhaustive parity/golden test
obligations yield to the owner's current policy. Actual feature requirements do
not disappear with those test inventories.

## Validation and durable evidence

The workspace's uv-created Python 3.14 environment imported both packages from
this worktree, not the main checkout's editable installs. No real KB was used.
Baseline collection: **1,713 nodes** plus seven module-level MinIO skips,
consistent with the supplied **1,709 passed / 11 skipped** audit baseline
(four runtime skips then). After deleting 225 nodes, the full run is:

```text
Focused 35 retained files, serial: 770 passed, 1 skipped in 99.19s (0:01:39)
uv run python -m pytest -q -n 4: 1486 passed, 9 skipped in 70.57s (0:01:10)
uv run python -m ruff check packages tests eval scripts: All checks passed!
git diff --check: clean
```

Nine skips: seven unconfigured MinIO modules, the optional onboarding MinIO
journey, and the unlisted private-app OKF alias test. Two private-app alias
nodes were removed with the complete migration smoke files. No new skips were
introduced and no failing test was discarded to obtain green.

Exact operational evidence is outside the disposable worktree under
`/tmp/lumio-test-reduction-20260907/`: `baseline-collection.txt`,
`retained-collection.txt`, `removed-node-ids.txt` (225 exact IDs),
`named-survivors.json`, `selection.json`, `orphan-cleanup.json`,
`focused-files.txt`, `focused-tests.txt`, `full-tests.txt`, and the final durable
`lane.patch`/handoff report. The final handoff records commit/tree/patch digest
and final checks; no ephemeral worktree is required to reconstruct the change.

Re-run exact collection and full gates from the candidate root:

```sh
uv run python -m pytest --collect-only -q -rs
uv run python -m pytest -q -n 4
uv run python -m ruff check packages tests eval scripts
git diff --check
```

For the focused serial run, use the exact 35 surviving test files named below
(all inventory files except deleted `test_foundation.py`/`test_primitives.py`):

```sh
uv run python -m pytest -q \
  tests/test_core_sdk_index.py \
  tests/test_zero_index_retrieval.py \
  packages/lumio-wiki/tests/test_entity_claims.py \
  packages/lumio-wiki/tests/test_entity_resolution.py \
  packages/lumio-wiki/tests/test_graph_structural_diagnostics.py \
  packages/lumio-wiki/tests/test_graph_state.py \
  packages/lumio-wiki/tests/test_graph_retrieval.py \
  packages/lumio-wiki/tests/test_discovery_graph.py \
  packages/lumio-wiki/tests/test_graph_exchange.py \
  packages/lumio-wiki/tests/test_retrieval_eval.py \
  eval/test_retrieval_eval_gate.py \
  eval/test_ontology_eval_gate.py \
  packages/lumio-lancedb/tests/test_graph_tables.py \
  packages/lumio-lancedb/tests/test_ontology_parity.py \
  packages/lumio-lancedb/tests/test_remote_index_binding.py \
  packages/lumio-lancedb/tests/test_remote_index_location.py \
  packages/lumio-wiki/tests/test_cli.py \
  packages/lumio-wiki/tests/test_cli_open_actions.py \
  packages/lumio-wiki/tests/test_cli_status.py \
  packages/lumio-wiki/tests/test_cli_s3.py \
  packages/lumio-wiki/tests/test_cli_remote_lance.py \
  packages/lumio-wiki/tests/test_cli_setup_s3.py \
  packages/lumio-wiki/tests/test_ingest_journey.py \
  packages/lumio-wiki/tests/test_managed_ingest.py \
  packages/lumio-wiki/tests/test_capture.py \
  packages/lumio-wiki/tests/test_capture_adapters.py \
  packages/lumio-wiki/tests/test_skill_management.py \
  packages/lumio-wiki/tests/test_skill_public_surface.py \
  packages/lumio-wiki/tests/test_wheel_isolation.py \
  packages/lumio-wiki/tests/test_wheel_isolation_llm.py \
  tests/test_onboarding_journey.py \
  packages/lumio-wiki/tests/test_source_lifecycle.py \
  packages/lumio-wiki/tests/test_source_resolution.py \
  packages/lumio-wiki/tests/test_source_inspection.py \
  packages/lumio-wiki/tests/test_review_after.py
```

Residual risks: all production audit findings remain open; reduced formatting,
representation, client/format cross-product and positive remote microcoverage is
intentional. No live MinIO/provider quality or crash atomicity is certified.
No runtime speedup is claimed from incomparable runs; the measured result above
is just this retained suite. No runtime sources, package metadata, CI, skills,
experiments or installed user instructions were changed.

## Exact removed functions, grouped by capability

Counts include function decorators and all parameter expansions. Class members
below in `test_retrieval_eval.py` belong to the removed
`TestDeterministicHashEmbedder` and `TestEvaluate` classes; exact class-qualified
node IDs are in the handoff artifact. All other listed names are file-level.

### Core loading, lookup and zero-index — 11 functions / 11 nodes

`tests/test_core_sdk_index.py` — 4 functions / 4 nodes:

- `test_freshness_reports_stale_after_source_change`
- `test_loaded_knowledge_base_builds_derived_index_once`
- `test_retrieval_result_source_type_is_free_string`
- `test_cli_retrieve_returns_cited_results`

`tests/test_zero_index_retrieval.py` — 2 functions / 2 nodes:

- `test_zero_index_adapter_build_writes_fingerprint`
- `test_kb_zero_index_retrieve_without_index_dir`

`packages/lumio-wiki/tests/test_foundation.py` — 3 functions / 3 nodes; complete file removed:

- `test_foundation_loads_validates_fingerprints_searches_reads_and_traverses`
- `test_foundation_regenerates_portable_artifacts`
- `test_legacy_knowledge_base_module_aliases_the_new_owner`

`packages/lumio-wiki/tests/test_primitives.py` — 2 functions / 2 nodes; complete file removed:

- `test_search_and_zero_index_retrieval_are_model_free`
- `test_legacy_primitive_modules_alias_the_new_owner`

### Entity/Claim and graph capabilities — 44 functions / 44 nodes

`packages/lumio-wiki/tests/test_entity_claims.py` — 2 functions / 2 nodes:

- `test_fingerprint_record_shape`
- `test_new_records_are_exported_from_lumio_wiki`

`packages/lumio-wiki/tests/test_entity_resolution.py` — 3 functions / 3 nodes:

- `test_resolve_entity_is_deterministic`
- `test_entity_command_resolves_by_id`
- `test_entity_id_for_title_returns_stable_id`

`packages/lumio-wiki/tests/test_graph_structural_diagnostics.py` — 13 functions / 13 nodes:

- `test_report_is_separate_type_from_graph_health_report`
- `test_deterministic_output_repeated_calls_equal`
- `test_hub_ordering_is_deterministic_with_title_tiebreak`
- `test_disconnected_components_counted`
- `test_duplicate_canonical_edges_counted_separately`
- `test_discovery_scope_retains_parallel_edges_to_same_endpoint`
- `test_empty_knowledge_base`
- `test_unresolved_groups_are_deterministically_ordered`
- `test_caller_retains_full_diagnostics_seam`
- `test_undirected_projection_disclosed_and_not_relationship_semantics`
- `test_self_relationship_excluded_from_structural_counts`
- `test_report_is_frozen_struct`
- `test_public_api_exports_structural_report_types`

`packages/lumio-wiki/tests/test_graph_state.py` — 11 functions / 11 nodes:

- `test_materialize_graph_creates_msgpack_artifact`
- `test_artifact_records_graph_version`
- `test_artifact_records_fingerprint_and_extractor_version`
- `test_load_or_derive_uses_artifact_when_fresh`
- `test_valid_claim_edge_decodes`
- `test_serialization_is_byte_identical_across_calls`
- `test_serialization_independent_of_page_insertion_order`
- `test_rebuilt_graph_matches_in_memory_derivation`
- `test_graph_health_empty_kb`
- `test_load_or_derive_is_idempotent`
- `test_public_exports_present`

`packages/lumio-wiki/tests/test_graph_retrieval.py` — 7 functions / 7 nodes:

- `test_trace_graph_stage_reports_seed_and_page_counts`
- `test_no_graph_params_behaves_exactly_as_before`
- `test_zero_index_graph_retrieval_works_without_lancedb`
- `test_graph_expansion_deterministic_across_calls`
- `test_trace_carries_resolved_seed_entity_ids`
- `test_trace_discloses_traversed_claim_ids_and_predicates`
- `test_trace_discloses_artifact_source_and_unresolved_seeds`

`packages/lumio-wiki/tests/test_discovery_graph.py` — 8 functions / 8 nodes:

- `test_relative_markdown_link_with_dot_slash_prefix_resolves`
- `test_nested_relative_link_prefers_source_dir_over_root`
- `test_markdown_link_resolves_via_alias`
- `test_wikilink_with_alias_label_resolves`
- `test_extraction_is_deterministic_for_unchanged_pages`
- `test_extraction_stable_across_repeated_calls`
- `test_extracted_references_module_function_returns_all`
- `test_discovery_scope_no_longer_raises`

### Graph exchange — 3 functions / 3 nodes

`packages/lumio-wiki/tests/test_graph_exchange.py` — 3 functions / 3 nodes:

- `test_export_graph_is_deterministic`
- `test_export_graph_matches_committed_gold_files`
- `test_import_graph_stub_matches_committed_gold_markdown`

### Evaluation and optional LanceDB — 21 functions / 21 nodes

`packages/lumio-wiki/tests/test_retrieval_eval.py` — 8 functions / 8 nodes:

- `test_same_input_same_output_across_instances`
- `test_dimension_and_model_info`
- `test_shared_tokens_are_more_similar_than_disjoint`
- `test_synonyms_collapse_paraphrases`
- `test_report_marks_graph_stage_skipped_for_unseeded_queries`
- `test_report_serializes_to_dict_and_table`
- `test_report_records_embedder_name_when_embedder_supplied`
- `test_report_embedder_is_none_without_embedder`

`eval/test_retrieval_eval_gate.py` — 3 functions / 3 nodes:

- `test_ac1_base_layer_needs_no_lancedb_or_embedder`
- `test_ac3_graph_disabled_is_the_zero_index_stage`
- `test_json_report_round_trips`

`eval/test_ontology_eval_gate.py` — 1 functions / 1 nodes:

- `test_report_discloses_corpus_mode_warm_up_and_fallback`

`packages/lumio-lancedb/tests/test_graph_tables.py` — 3 functions / 3 nodes:

- `test_entity_fts_candidate_retrieval`
- `test_scalar_status_and_kind_filters`
- `test_disputed_claims_never_enter_loaded_adjacency`

`packages/lumio-lancedb/tests/test_ontology_parity.py` — 4 functions / 4 nodes:

- `test_unknown_predicate_in_corpus_blocks_validation`
- `test_domain_violation_in_corpus_blocks_validation`
- `test_redirect_cycle_in_corpus_blocks_validation`
- `test_missing_evidence_section_blocks_validation`

`packages/lumio-lancedb/tests/test_remote_index_binding.py` — 1 functions / 1 nodes:

- `test_bound_location_and_fingerprint_are_readable`

`packages/lumio-lancedb/tests/test_remote_index_location.py` — 1 functions / 1 nodes:

- `test_as_location_normalizes_path_str_and_passthrough`

### CLI configuration, read/actions and remote status — 69 functions / 69 nodes

`packages/lumio-wiki/tests/test_cli.py` — 38 functions / 38 nodes:

- `test_build_parser_produces_lumio_wiki_prog`
- `test_no_command_prints_help_and_returns_1`
- `test_version_flag_prints_package_version`
- `test_all_documented_commands_have_handlers`
- `test_init_creates_control_file_and_derived_dirs`
- `test_validate_valid_knowledge_base_returns_0`
- `test_search_returns_matching_pages`
- `test_search_output_exposes_stable_entity_ids`
- `test_page_reads_by_canonical_title`
- `test_page_falls_back_to_alias`
- `test_related_lists_outgoing_titles`
- `test_paths_finds_shortest_path`
- `test_ingest_stages_reviewable_proposal`
- `test_proposal_list_shows_staged_proposal`
- `test_proposal_inspect_prints_metadata_and_diff`
- `test_proposal_inspect_json_emits_valid_json`
- `test_proposal_validate_reports_valid`
- `test_ingest_distiller_passthrough_is_the_default`
- `test_health_reports_page_count_and_graph`
- `test_doctor_reports_version_optionals_and_skill`
- `test_skill_path_prints_existing_skill`
- `test_skill_protocol_prints_existing_protocol`
- `test_skill_install_copies_into_agent_dir`
- `test_paths_trace_reports_found_and_hops`
- `test_hot_prints_pinned_hot_index`
- `test_hot_reports_when_no_pins`
- `test_index_prints_root_navigation_index`
- `test_index_prints_subdirectory_index`
- `test_kb_path_defaults_to_env_var`
- `test_explicit_path_overrides_env_var`
- `test_setup_with_skill_install`
- `test_setup_env_var_enables_implicit_path`
- `test_env_file_loaded_when_no_positional_and_no_env_var`
- `test_positional_path_overrides_env_file_and_env_var`
- `test_exported_env_var_overrides_env_file`
- `test_setup_help_claims_env_loading`
- `test_module_usage_documents_optional_kb_path`
- `test_kb_path_help_documents_env_and_env_file`

`packages/lumio-wiki/tests/test_cli_open_actions.py` — 7 functions / 7 nodes:

- `test_search_prints_copyable_open_command`
- `test_search_hides_browser_links_without_a_configured_base_url`
- `test_search_prints_reader_url_when_base_url_configured`
- `test_page_prints_open_command_and_reader_url`
- `test_page_keeps_existing_grounding_lines_stable`
- `test_evidence_renderer_prints_labelled_open_actions`
- `test_evidence_renderer_without_kb_or_base_url_stays_concise`

`packages/lumio-wiki/tests/test_cli_status.py` — 13 functions / 13 nodes:

- `test_status_maintainer_role_and_argument_source`
- `test_status_config_source_exported_env`
- `test_status_config_source_project_env_file`
- `test_status_backend_and_mode_are_separate_fields`
- `test_status_rendered_output_distinguishes_backend_and_mode`
- `test_status_graph_memory_then_artifact_after_materialization`
- `test_status_zero_index_backend_never_probes_lance`
- `test_status_json_matches_rendered_keys`
- `test_status_reader_reports_published_version_and_fingerprint`
- `test_status_reader_graph_derived_in_memory`
- `test_status_reader_lance_healthy`
- `test_setup_prints_shared_status_summary_maintainer`
- `test_setup_prints_shared_status_summary_reader`

`packages/lumio-wiki/tests/test_cli_s3.py` — 6 functions / 6 nodes:

- `test_cli_validate_s3_uri_routes_through_the_snapshot`
- `test_cli_search_s3_uri_returns_results`
- `test_cli_page_s3_uri_reads_a_compiled_page`
- `test_cli_related_s3_uri_traverses_the_graph`
- `test_cli_paths_s3_uri_finds_a_path`
- `test_cli_doctor_reports_the_s3_extra`

`packages/lumio-wiki/tests/test_cli_remote_lance.py` — 2 functions / 2 nodes:

- `test_retrieval_backend_defaults_to_zero_index`
- `test_retrieval_backend_reads_exported_env`

`packages/lumio-wiki/tests/test_cli_setup_s3.py` — 3 functions / 3 nodes:

- `test_load_project_config_exported_values_beat_env_file`
- `test_generated_agents_md_documents_configured_s3_settings`
- `test_setup_help_documents_the_s3_forms`

### Ingestion and capture — 10 functions / 18 nodes

`packages/lumio-wiki/tests/test_ingest_journey.py` — 2 functions / 2 nodes:

- `test_public_surface_exposes_the_journey_interfaces`
- `test_full_journey_runs_end_to_end_through_lumio_wiki`

`packages/lumio-wiki/tests/test_managed_ingest.py` — 1 functions / 5 nodes:

- `test_managed_ingest_records_original_provenance_per_format`

`packages/lumio-wiki/tests/test_capture.py` — 5 functions / 9 nodes:

- `test_load_manifest_reads_client_manifest_and_keeps_bytes`
- `test_detect_raw_transcript_finds_chat_turns`
- `test_each_client_fixture_captures_through_one_contract`
- `test_capture_never_autopublishes`
- `test_cli_capture_help_lists_session_subcommand`

`packages/lumio-wiki/tests/test_capture_adapters.py` — 2 functions / 2 nodes:

- `test_codex_discovery_orders_and_projects`
- `test_manifest_fixture_round_trips_through_capture_contract`

### Skill installation, onboarding and packaging — 24 functions / 27 nodes

`packages/lumio-wiki/tests/test_skill_management.py` — 4 functions / 7 nodes:

- `test_shared_scope_destinations`
- `test_vendor_compatibility_destinations`
- `test_install_writes_traceable_manifest_and_reports_current`
- `test_cli_install_and_status_shared_user_scope`

`packages/lumio-wiki/tests/test_skill_public_surface.py` — 13 functions / 13 nodes:

- `test_skill_uses_portable_agent_skills_frontmatter_and_relative_protocol`
- `test_every_cited_command_is_a_registered_public_cli_command`
- `test_skill_management_and_setup_are_in_parity_across_public_surfaces`
- `test_skill_documents_the_full_retrieval_ladder`
- `test_open_action_labels_are_documented_across_all_public_surfaces`
- `test_setup_is_canonical_first_run_across_all_public_surfaces`
- `test_generated_agents_md_guides_a_restarted_session`
- `test_surfaces_distinguish_extracted_references_from_typed_relationships`
- `test_real_world_readme_names_exact_setup_and_restart_check`
- `test_command_coverage_parity_for_relationship_and_source_lifecycle`
- `test_s3_setup_forms_parity_across_public_surfaces`
- `test_s3_journey_is_documented_across_public_surfaces`
- `test_doctor_guides_the_s3_journey_install`

`packages/lumio-wiki/tests/test_wheel_isolation.py` — 1 functions / 1 nodes:

- `test_isolated_install_has_no_heavyweight_dependencies`

`packages/lumio-wiki/tests/test_wheel_isolation_llm.py` — 1 functions / 1 nodes:

- `test_base_wheel_does_not_import_openai`

`tests/test_onboarding_journey.py` — 5 functions / 5 nodes:

- `test_quickstart_cites_registered_public_commands`
- `test_journey_commands_have_one_home_across_quickstart_script_and_example`
- `test_entrypoint_docs_link_the_canonical_journey`
- `test_skill_documents_the_s3_journey_commands`
- `test_quickstart_explains_the_four_operating_model_facts`

### Private source lifecycle and inspection — 28 functions / 31 nodes

`packages/lumio-wiki/tests/test_source_lifecycle.py` — 16 functions / 19 nodes:

- `test_public_surface_exposes_serializable_source_lifecycle_records`
- `test_ingest_store_keeps_private_registry_outside_raw_and_proposal_trees`
- `test_pipeline_facing_lifecycle_contracts_are_public_but_registry_is_private`
- `test_list_sources_exposes_registered_identities_through_the_pipeline`
- `test_list_sources_without_store_returns_empty`
- `test_list_retirement_candidates_exposes_recorded_candidates_through_pipeline`
- `test_list_retirement_candidates_without_store_returns_empty`
- `test_public_surface_exposes_safe_candidate_trigger_display`
- `test_safe_candidate_trigger_display_returns_recognized_verbatim`
- `test_public_surface_exposes_safe_lifecycle_trigger_display`
- `test_safe_lifecycle_trigger_display_maps_retire_action`
- `test_safe_lifecycle_trigger_display_maps_reactivate_action`
- `test_safe_lifecycle_trigger_display_never_echoes_trigger_material`
- `test_public_surface_exposes_safe_lifecycle_impact_status_display`
- `test_safe_lifecycle_impact_status_display_passes_still_supported`
- `test_safe_lifecycle_impact_status_display_passes_sole_source_lost`

`packages/lumio-wiki/tests/test_source_resolution.py` — 9 functions / 9 nodes:

- `test_suggest_matches_a_page_title_shaped_query_to_the_source_id`
- `test_suggest_matches_substring_prefixes_and_is_sorted`
- `test_resolve_exact_known_source_id_wins_first`
- `test_resolve_by_entity_id`
- `test_resolve_by_canonical_title`
- `test_resolve_by_alias`
- `test_resolve_by_page_path`
- `test_resolve_duplicate_titles_declaring_one_source_resolve`
- `test_resolve_unknown_input_carries_bounded_suggestions`

`packages/lumio-wiki/tests/test_source_inspection.py` — 3 functions / 3 nodes:

- `test_parse_expires_accepts_units_and_bare_minutes`
- `test_resolve_registry_binding_returns_current_version`
- `test_fetch_verified_artifact_returns_byte_exact_original`

### Review-after maintenance — 1 functions / 1 nodes

`packages/lumio-wiki/tests/test_review_after.py` — 1 functions / 1 nodes:

- `test_status_rendered_output_includes_review_due`

### Orphan exchange goldens

- `tests/fixtures/graph_exchange/graph.json` — 452 lines.
- `tests/fixtures/graph_exchange/graph.graphml` — 272 logical lines.
- `tests/fixtures/graph_exchange/transformers_stub.md` — 21 lines.

Repository reference search found consumers only in the two removed golden
checks. Semantic JSON/GraphML/import and visibility journeys remain intact.
