# Library stabilization and simplification

Status: **plans 02–05 are proposed, not approved for execution**. Only the
[test reduction](01-test-retention.md) is implemented here. No production defect
is fixed by this batch. Audit revision: `9712f6fd51ac4381a6ca4fc513e2546355fba0d0`;
implementation base: `67b45ab4ac114d674ec900c7f17b171eae5e92fa`.

## Owner decisions and authority

- Keep essential journeys plus distinct security, data-loss, isolation,
  validation, conflict and resource-bound regressions. Actually delete tests;
  do not conceal cases in loops, alter collection, or weaken survivors.
  Incidental formatting, representation, exhaustive exports and noncritical
  edge coverage may be lost. This supersedes older gold-file/wording-parity
  obligations, not the underlying supported capabilities.
- Preserve optional converters, providers, capture clients, S3 and LanceDB.
  Host-as-Distiller stays the default; providers remain opt-in.
- A coordinated breaking API release is allowed, **after actual consumer
  inventory and migration**, including the separate private Lumio application.
  Absence of a caller in this repository does not prove an API dead.
- Serialize cooperating local writers per Knowledge Base across processes;
  roll back ordinary failures. Crash atomicity and atomic visibility to
  concurrent readers are not promised. External manual edits do not obey a lock.
- Disjoint staged proposals may publish after intervening disjoint changes.
  Check affected-file and control preconditions, then validate the full current
  candidate; do not reject all outstanding proposals using a whole-KB digest.
- Plans belong here. No tickets, push, release, or main-branch integration is
  authorized. Local lane commits are solely durable review handoffs. Plans
  02–05 require separate implementation approval; S4 is a separate release gate.

Sources: [Core PRD](../prd/0002-core-sdk.md), [glossary](../../CONTEXT.md),
[packaging ADR-0010](../adr/0010-uv-workspace-and-progressive-packaging.md),
[graph ADR-0011](../adr/0011-derived-reference-graph-and-progressive-storage.md),
[S3 ADR-0013](../adr/0013-s3-native-knowledge-base-locations.md),
[source lifecycle ADR-0014](../adr/0014-source-versions-and-page-level-invalidation.md),
[skill ADR-0017](../adr/0017-portable-agent-skill-distribution-and-project-bootstrap.md),
[converters ADR-0018](../adr/0018-layered-document-conversion-anydoc.md),
[ontology ADR-0021](../adr/0021-entity-claim-ontology-and-progressive-graph-materialization.md),
[Activity Log ADR-0022](../adr/0022-retire-activity-log-artifact.md),
[exchange ADR-0024](../adr/0024-graph-exchange-and-cli-skill-boundary.md), and
[split ADR-0025](../adr/0025-repository-split.md).
[ADR-0019](../adr/0019-coding-agent-setup-and-complete-s3-publication.md) and
[ADR-0020](../adr/0020-private-source-artifacts-and-authorized-inspection.md)
remain **proposed**: their shipped behavior/tests are capability evidence, not
accepted architectural authority. ADR-0010 supersedes the old PRD's ingestion
exclusion; ADR-0021 supersedes title-based Relationship input. The current owner
bounds local atomicity more narrowly than ADR-0021's broad atomicity wording.

Evidence provenance: the complete original audit is
`/tmp/lumio-wiki-audit-9712f6f.md`; the approved deletion maps are the **final**
`core.md` and `workflows.md` under session artifact run
`95588354-ff5d-469d-afe4-bd675af92c90/test-reduction/`.
These are review artifacts, not portable dependencies of the product. The tables
below record their findings with code owners verified at the pinned base; the
retention plan contains the applied list. No original repro was rerun or fixed
in this documentation lane. Future tasks first reproduce against their own base.

## Order and ownership

1. [01 — Test retention](01-test-retention.md): implemented reduction and evidence.
2. [02 — Publication integrity](02-publication-integrity.md): P1–P5, one mutation owner.
3. [03 — Snapshot/retrieval](03-snapshot-retrieval.md): R1–R6, one captured identity.
4. [04 — Agent workflows](04-agent-workflows.md): A1–A7, safety before additions.
5. [05 — Simplification/release](05-simplification-release.md): S1–S4, migration before deletion.

After new approval, integrate P1/P2 → P3 → P4 → P5; R1 → R2/R3 → R4/R5 → R6;
A1/A2/A4 and A3 safety repair → A5/A6/A7 additions; consumer inventory precedes
S1/S2 API removals, R1 precedes S3, all fixes precede S4. A3 may centralize
composition after its ingestion repair; S2 removes migrated old seams, not a
second implementation. Shared `knowledge_base.py`, `ingest.py`,
`proposal_pipeline.py`, `cli.py`, `__init__.py` and fixtures require serial
integration even if independent work is developed separately. One writer per
worktree; no automatic merge or release.

## Finding-to-task map

Paths in this table abbreviate **W** = `packages/lumio-wiki/src/lumio_wiki/`,
**L** = `packages/lumio-lancedb/src/lumio_lancedb/`. Symbols rather than stale
line numbers identify the inspected owners. B01–B18 are audit-observed failures
(including disclosed fault/race injections), except where qualified below.

| ID | Evidence / qualification | Task and completion evidence |
| --- | --- | --- |
| B01 | `W/publish.py:apply_proposed_pages`, new `A_B` overwrites `A B` at `a_b.md` | P2: occupied and duplicate targets blocked; same-page revision works |
| B02 | `W/source_registry.py:SourceRegistry._commit`, two instances lose the first registration | P3: lock/reload transaction survives separate processes |
| B03 | `W/ingest.py:IngestStore.get`, cached discarded proposal publishes | P3/P4: reread durable terminal state under shared lock |
| B04 | `W/proposal_pipeline.py:ProposalPipeline.publish`, older overlapping proposal overwrites newer page | P4: reviewed affected-path/control bases enforced; disjoint proposals succeed |
| B05 | Same method plus `W/publish.py`, injected second target write failure leaves first changed | P5: ordinary-failure restoration of complete mutation state |
| B06 | `W/knowledge_base.py:_as_claims`, `_load_page`, `_load_pages_and_validate`: issues discarded, nonnumeric confidence raises | P1: structural errors aggregate, no silent malformed-Claim acceptance |
| B07 | `W/knowledge_base.py:_ontology_issues`: mapping literal and `[0,0]` anchors accepted | P1: scalar kinds and 1-based coordinates enforced |
| B08 | `KnowledgeBase.build_index`, `retrieve`, `materialize_graph`: old pages stamped with live disk hash | R1: bytes/pages/graph/index share captured identity |
| B09 | `L/index.py:LanceDBRetrievalAdapter.build_index`: lexical rebuild leaves old vectors/model metadata | R2: invalid semantic state cannot cite stale/deleted content |
| B10 | `W/s3_publish.py:publish_s3_version`, `W/artifact_store.py:activation_binding_hook`: deterministic capture/binding races | R3: content, digest, graph, index and private bindings use one candidate; retain CAS |
| B11 | `W/artifact_store.py:verify_rollback_coverage`: optional empty manifest later passes required rollback | R4: reproduce partial-manifest hole first; compare complete historical required set |
| B12 | `W/evidence.py:body_sections`, `page_evidences`: nested headings skipped, duplicate whole-page hits | R5: focused nested passage, exact range, useful snippet and result budget |
| B13 | `W/capture.py:redact_capture_text`: quoted JSON password reaches staged page | A1: quoted/escaped keys redacted before preview and staging |
| B14 | `W/url_fetch.py:_request_once`, `fetch_url`: loopback slow header exceeds per-hop deadline | A2: remaining budget applies through connect/headers/body; disclose DNS ceiling |
| B15 | `W/cli.py:_cmd_ingest` excludes AnyDoc from wrapping; SDK includes it | A3: real CSV CLI reaches a proposal via shared preparation |
| B16 | `W/citation_actions.py:page_open_command`, `source_inspect_command`: explicit KB dropped | A4: executable actions replay the effective original location |
| B17 | `W/distiller.py:OpenAIDistiller._call_with_retry`, `W/cli.py:main`: raw exception and traceback escape | A1: bounded safe errors; synthetic provider proved propagation, real secret content is conditional |
| B18 | `W/data/skill/SKILL.md`, `PROTOCOL.md`, `README.md`: source ordering/skill syntax and invalid initialized-v2 sample | A5/A7: executed schema and packaged command examples |
| B19 | `W/proposal_pipeline.py:publish`, `W/knowledge_base.py:append_activity_log_entry`: reachable writer contradicts ADR-0022 | S1: retire producers, not legacy recognition/collision guards |
| B20 | `W/__init__.py`, `KnowledgeBase.graph_path`, `shortest_path`, `_load_and_validate` | S2/S3: actual caller migration and one traversal owner; no measured scale defect claimed |
| B21 | `W/retrieval_eval.py:lancedb_available`, `load_lancedb_adapter`; CLI runtime routes import evaluation | A3/S2: one optional composition owner, not a plugin framework |
| I01 | `W/cli.py:_cmd_page` is lossy human output; JSON coverage selective | A6: optional bounded raw/JSON reads and stable machine mutation/error results |
| I02 | Distiller prompt has categories but no current ontology; onboarding leads with S3 | A5/A7: KB-aware guidance and local-first journey |
| I03 | `eval/gold_set.yaml`, starter gold set: no empty-relevance queries; permissive enhanced gate | R6: hard negatives, passages and source/index-mode lifecycle evaluation |
| I04 | Packaged protocol lacks explicit source-instruction authorization boundary | A7: source/page/transcript/tool output are untrusted data |
| C01 | `CONTEXT.md` foregrounds private UI, duplicates trailing terms, stale Relationship/Activity Log language; PRD scope lags | A5/S1/S4: align vocabulary/docs without deleting private-app capabilities |
| C02 | `W/embeddings.py:cosine_similarity`, `is_semantic_index_stale`, exported `DocumentSourceProcessor`, `L/graph.py:load_graph_state` lack local production callers | S2: inventory candidates only, not proven dead APIs |
| C03 | Graph materializations do not feed built-in traversal; validation index discarded | S3: reuse captured facts and measure, not a new generic graph engine |
| C04 | Converter/OCR/page-boundary, optional provider, skill-install and remote capabilities are legitimate | A1–A3/S2/S4: preserve missing-extra guidance, isolation, converter provenance and installed gates |

Non-findings remain boundaries: no demonstrated SSRF bypass, environment shell
execution, or private-app publisher defect; no proof that optional base imports
leak. Page search is not Evidence retrieval; graph reachability is not support;
citation existence is not entailment. Keep Hot and Navigation Indexes distinct,
MessagePack and LanceDB progressive projections, source-version inspection,
visibility-before-expansion, and skill-install consent/concurrency safeguards.
No mandatory dependencies, database, generic storage/plugin framework, new agent
runtime, automatic Claim publication or distribution. MCP requires a concrete
client and separate approval, not this plan set.

## Global verification for future implementation

Python ≥3.14, uv workspace, existing pytest/Ruff. Every task names its exact
focused files; run those serially. For each `new-test` task, add the named missing
regression, observe its intended red failure, implement the smallest fix, rerun
that node green and then its focused files. Existing journeys should carry the
rest; do not recreate the removed matrices.

```sh
uv run python -m pytest -q -n 4
uv run python -m ruff check packages tests eval scripts
uv lock --check
git diff --check
```

Verify imports point into the worktree being tested. Use `python -m pytest`, not
a potentially stale pytest launcher, and never `-n auto`. Installed-wheel tests
already run in the full suite; [S4](05-simplification-release.md#s4) additionally
certifies the explicit CI smoke scripts and consumer artifacts for release.
MinIO requires an explicitly provisioned disposable service; skips are not live
S3 evidence. No real KB or installed user skill is a fixture. Plan-writing itself
is `no-new-test`: local links, exact paths/symbols/task IDs and placeholders are
checked, Markdown lint only if already available. Approval of these documents
alone is not implementation or release approval.
