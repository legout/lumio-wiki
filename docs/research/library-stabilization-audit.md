# Library stabilization audit evidence

_Date: 2026-09-07. Audit revision: `9712f6fd51ac4381a6ca4fc513e2546355fba0d0`. Evidence only; behavior is owned by [PRD-0006](../prd/0006-library-stabilization.md), and execution is owned by `docs/plans/`._

## Provenance

The complete original audit is `/tmp/lumio-wiki-audit-9712f6f.md`. The approved test-deletion maps are the final `core.md` and `workflows.md` under session artifact run `95588354-ff5d-469d-afe4-bd675af92c90/test-reduction/`. These are review artifacts, not portable product dependencies. No original reproduction was rerun or fixed in this documentation pass; implementation must reproduce a finding against its own base.

Paths below abbreviate **W** = `packages/lumio-wiki/src/lumio_wiki/` and **L** = `packages/lumio-lancedb/src/lumio_lancedb/`. B01–B18 are audit-observed failures, including disclosed fault/race injections, except where qualified.

## Finding registry

| ID | Evidence / qualification |
| --- | --- |
| B01 | `W/publish.py:apply_proposed_pages`: new `A_B` overwrites `A B` at `a_b.md` |
| B02 | `W/source_registry.py:SourceRegistry._commit`: two instances lose the first registration |
| B03 | `W/ingest.py:IngestStore.get`: cached discarded proposal publishes |
| B04 | `W/proposal_pipeline.py:ProposalPipeline.publish`: older overlapping proposal overwrites newer page |
| B05 | `W/publish.py`: injected second target write failure leaves the first target changed |
| B06 | `W/knowledge_base.py:_as_claims`, `_load_page`, `_load_pages_and_validate`: issues discarded; nonnumeric confidence raises |
| B07 | `W/knowledge_base.py:_ontology_issues`: mapping literal and `[0,0]` anchors accepted |
| B08 | `KnowledgeBase.build_index`, `retrieve`, `materialize_graph`: old pages stamped with a live-disk hash |
| B09 | `L/index.py:LanceDBRetrievalAdapter.build_index`: lexical rebuild leaves old vectors/model metadata |
| B10 | `W/s3_publish.py:publish_s3_version`, `W/artifact_store.py:activation_binding_hook`: deterministic capture/binding races |
| B11 | `W/artifact_store.py:verify_rollback_coverage`: optional empty manifest later passes required rollback |
| B12 | `W/evidence.py:body_sections`, `page_evidences`: nested headings skipped and whole-page hits duplicated |
| B13 | `W/capture.py:redact_capture_text`: quoted JSON password reaches a staged page |
| B14 | `W/url_fetch.py:_request_once`, `fetch_url`: loopback slow header exceeds the per-hop deadline |
| B15 | `W/cli.py:_cmd_ingest` excludes AnyDoc wrapping while the SDK includes it |
| B16 | `W/citation_actions.py:page_open_command`, `source_inspect_command`: explicit KB location is dropped |
| B17 | `W/distiller.py:OpenAIDistiller._call_with_retry`, `W/cli.py:main`: raw exception and traceback escape |
| B18 | `W/data/skill/SKILL.md`, `PROTOCOL.md`, `README.md`: source ordering/skill syntax and initialized-v2 sample are invalid |
| B19 | `W/proposal_pipeline.py:publish`, `W/knowledge_base.py:append_activity_log_entry`: reachable writer contradicts ADR-0022 |
| B20 | `W/__init__.py`, `KnowledgeBase.graph_path`, `shortest_path`, `_load_and_validate`: duplicate or candidate seams require consumer inventory |
| B21 | `W/retrieval_eval.py:lancedb_available`, `load_lancedb_adapter`: optional composition ownership is duplicated |
| I01 | `W/cli.py:_cmd_page` is lossy human output; JSON coverage is selective |
| I02 | Distiller prompt has categories but no current ontology; onboarding leads with S3 |
| I03 | Evaluation sets have no empty-relevance queries; enhanced gate is permissive |
| I04 | Packaged protocol lacks an explicit source-instruction authorization boundary |
| C01 | `CONTEXT.md` foregrounds private UI and had stale Relationship/Activity Log language; PRD scope lags |
| C02 | Several exported helpers lack local production callers; absence here does not prove them dead externally |
| C03 | Graph materializations do not feed built-in traversal; the validation index is discarded |
| C04 | Converter/OCR/page-boundary, optional provider, skill-install, S3, and LanceDB capabilities are legitimate and must remain optional |

## Non-findings and limits

The audit did not demonstrate an SSRF bypass, environment shell execution, a private-app publisher defect, or optional base-import leakage. Page search is not Evidence retrieval; graph reachability is not support; citation existence is not entailment. Hot and Navigation Indexes remain distinct. MessagePack and LanceDB remain progressive projections. Visibility is enforced before expansion. No mandatory database, generic storage/plugin framework, new agent runtime, automatic Claim publication, or automatic distribution is justified.

The audit observed ordinary-failure and cooperating-writer defects; it did not establish crash atomicity, atomic visibility to concurrent readers, live cloud permissions, or real embedding quality. MinIO/provider certification requires an explicitly provisioned environment and separate evidence.
