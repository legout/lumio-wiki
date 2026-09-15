# Plan 05 consumer API inventory

_Date: 2026-09-15. Accepted by the owner as Plan 05’s consumer baseline on 2026-09-15. Research evidence only; not implementation, private-repository mutation, API-removal, or release approval._

## Scope, revisions, and limitations

Read-only inspection covered only the two owner-confirmed repositories:

- public foundation `/home/volker/coding/lumio-wiki` at `4948aca70ac4d469bb8514dac29ef6079629578c`;
- private application `/home/volker/coding/lumio` at `cf7fd293c717f6efbbbd592137ab7ebeddffd589`.

`git status --porcelain` returned no output in either repository before inspection. No repository file was changed. I excluded `.git`, nested `.worktrees`, `.wiki`, `__pycache__`, `.pytest_cache`, `node_modules`, `dist`, `build`, lockfile-generated package records, and other generated/vendor artifacts from consumer conclusions. Stale `.pyc` files mentioning deleted MinIO tests were observed but are not source evidence.

This is **planning-contract v1 research evidence**, not approval to implement, migrate, remove, release, or edit the private app. Search absence proves only “no match in these two revisions and searched source sets,” never absence of unconfirmed external consumers. Dynamic imports, wildcard re-exports, string references, package metadata, and entry points were inspected explicitly; arbitrary reflection outside these repositories cannot be disproved.

Authoritative inputs read: both `AGENTS.md`; both `CONTEXT.md`; public Core PRD; private platform PRD; Plan 05; PRD-0006; stabilization audit; ADR-0022; ADR-0025; and artifact/planning classification. Key authority: removal requires real consumer migration, and absence locally is insufficient (`docs/prd/0006-library-stabilization.md:11-14,30-38`); Plan 05 remains proposed/unapproved and explicitly blocks S1/S2 removals and S4 (`docs/plans/05-simplification-release.md:1-23`). ADR-0022 requires writer deletion but legacy marked-log recognition and collision protection (`docs/adr/0022-retire-activity-log-artifact.md:31-48,67-76`). ADR-0025 makes the private app a bounded-range wheel consumer and cross-repo migration participant (`docs/adr/0025-repository-split.md:29-57`).

## Accepted-baseline addendum

Shaping exposed a documentation-authority conflict in the private application:
its accepted ADR-0022 retired Activity Log production, while its glossary,
Knowledge Base format, packaging guide, and older ADR text still presented the
portable log as current. The owner chose ADR-0022. Private docs-only merge
`43facf97fc40497688b54546b511c5bd392396a6` removes Activity Log as current
vocabulary, records only validly marked legacy-log recognition, and marks older
ADR references as historical. It does not change production code. The active
private writer in `packages/lumio/src/lumio/app.py` therefore remains a hard S1
migration blocker.

## Methodology and copyable commands

```sh
git -C /home/volker/coding/lumio-wiki rev-parse HEAD
git -C /home/volker/coding/lumio-wiki status --porcelain
git -C /home/volker/coding/lumio rev-parse HEAD
git -C /home/volker/coding/lumio status --porcelain

cd /home/volker/coding/lumio-wiki
rg -n --glob '!uv.lock' --glob '!docs/**' --glob '!**/__pycache__/**' \
 'parse_frontmatter|as_sources|graph_path|shortest_path|load_graph_state|DocumentSourceProcessor|cosine_similarity|is_semantic_index_stale|load_lancedb_adapter|append_activity_log_entry|make_activity_log_entry|_format_activity_log_line' \
 packages tests eval scripts examples
rg -n '^\s*(from lumio_(wiki|lancedb)|import lumio_(wiki|lancedb))' packages/*/src --glob '*.py'

cd /home/volker/coding/lumio
rg -n '^\s*(from lumio_(wiki|lancedb)|import lumio_(wiki|lancedb))' packages/lumio/src --glob '*.py'
rg -n --glob '!uv.lock' --glob '!docs/**' --glob '!**/.wiki/**' --glob '!**/.worktrees/**' \
 'parse_frontmatter|as_sources|graph_path|shortest_path|load_graph_state|DocumentSourceProcessor|cosine_similarity|is_semantic_index_stale|load_lancedb_adapter|append_activity_log_entry|make_activity_log_entry|_format_activity_log_line' \
 packages/lumio/src tests
```

## Files Retrieved

1. `docs/plans/05-simplification-release.md` (lines 1-224) — unapproved sequence, candidate list, gates, and stale S4 commands.
2. `docs/prd/0006-library-stabilization.md` (lines 1-42) — approved behavior and external-consumer requirement.
3. `docs/research/library-stabilization-audit.md` (lines 1-49) — B19-B21/C01-C04 evidence and limitations.
4. `docs/adr/0022-retire-activity-log-artifact.md` (lines 1-76) — Activity Log retirement contract.
5. `docs/adr/0025-repository-split.md` (lines 1-66) — ownership, dependency direction, and bounded ranges.
6. `packages/lumio-wiki/src/lumio_wiki/__init__.py` (lines 1-391, 404-755) — broad root imports and `__all__`; candidate aliases/exports.
7. `packages/lumio-wiki/src/lumio_wiki/knowledge_base.py` (lines 102-151, 445-470, 700-960, 2528-2610, 4696-4710, 5360-5430) — constants, parser/source definitions, duplicate index build, traversal APIs, Activity Log producers.
8. `packages/lumio-wiki/src/lumio_wiki/location.py` (lines 134-405) — snapshot façade duplicating SDK traversal and canonical Location opening seam.
9. `packages/lumio-wiki/src/lumio_wiki/source_processor.py` (lines 1-160, 457 onward) — source-processing owner and document adapter.
10. `packages/lumio-wiki/src/lumio_wiki/embeddings.py` (lines 78-147) — vector helper and stale-model helper.
11. `packages/lumio-wiki/src/lumio_wiki/composition.py` (lines 1-66) — optional-adapter discovery owner and dynamic import.
12. `packages/lumio-lancedb/src/lumio_lancedb/graph.py` (lines 421-505) — LanceDB graph-state projection loader.
13. `packages/lumio-lancedb/src/lumio_lancedb/__init__.py` (lines 1-67) — adapter root exports/version.
14. `packages/lumio-wiki/src/lumio_wiki/proposal_pipeline.py` (lines 1920-2026) — active public production Activity Log calls and rollback coupling.
15. `packages/lumio/src/lumio/app.py` (lines 19-57, 1980-2020) — private root imports and second active Activity Log producer path.
16. `packages/lumio/src/lumio/ingest.py` (lines 39-91, 158-162, 384-484) — private duplicate converter seam and private parser import/calls.
17. `packages/lumio/src/lumio/chat_history.py` (lines 39-41, 1205-1242) — private production cosine consumer.
18. `packages/lumio/src/lumio/runtime.py` (lines 7-20, 491-510) — private production bounded traversal consumer.
19. `packages/lumio/src/lumio/core/__init__.py` (lines 1-8) and `core/index.py` (lines 1-24) — wildcard SDK and LanceDB compatibility shims.
20. `scripts/verify_lumio_wiki_wheel.py` (lines 110-115) — installed-wheel consumer of both Activity Log producer APIs.
21. Public/private package `pyproject.toml` files — exact `0.1.2` versions, dependency ranges, extras, and entry points.

## Key Code

### Evidence table

| Candidate | Definition / owner; export | Observed consumers | Replacement seam / action | Confidence and unknowns |
|---|---|---|---|---|
| `parse_frontmatter` | Private implementation `_parse_frontmatter` in `knowledge_base.py:2528`; renamed root export at `__init__.py:135-140`, `__all__` at `:717`. | Public production calls it in `knowledge_base.py:2971,4803,5187`; `ingest.py:1002,1045,1064,1560,1570,1697`; `okf.py:1514,1533,1675`; `proposal_pipeline.py:245,1127,1341,1383,1388`; and `publish.py:1664,1666` (the latter two modules use local aliases). **Private production directly imports `_parse_frontmatter`** in `packages/lumio/src/lumio/ingest.py:84-89` and calls it at `:384,484`. No confirmed private use of the root alias. Tests/docs also exercise parsing indirectly/directly. | Migrate parser calls to an approved owned ingest/page parsing seam before narrowing. Root alias may be **remove after migration**, but the private direct-private import proves parser behavior remains needed. | High for these repos. Unknown external root-alias consumers block deletion.
| `as_sources` | `_as_sources` at `knowledge_base.py:2594`; root rename/export `__init__.py:135-137,417`. | Public production calls it in `knowledge_base.py:2985`; `ingest.py:1169,1577`; `okf.py:1772,1801`; and `publish.py:1670-1671` (through a local alias). No private production match for either spelling. | Keep internal owner while callers need it; root alias **remove after migration** only after external inventory. | High local; external unknown blocks removal.
| `KnowledgeBase.graph_path` | Legacy unbounded BFS at `knowledge_base.py:746-782`; not a root function but public method. Snapshot delegates at `location.py:286-287`. | No public production caller found beyond façade; tests at private `tests/test_core_sdk.py:241-244` and `test_core_without_lancedb.py:111`. No private production call. | `shortest_path` is observed modern bounded seam, but semantics differ (alias resolution, authorization, same/missing endpoints, bounds) per code at `knowledge_base.py:883-960` and Plan S3. **Migrate**, then **remove after migration** only with semantic decision and external evidence. | High local; removal blocked by legacy tests and unknown external method consumers.
| `shortest_path` | Bounded authorized BFS at `knowledge_base.py:883-960`; Snapshot delegate `location.py:312-323`. | Public CLI production `cli.py:1940`. Private runtime production `packages/lumio/src/lumio/runtime.py:491-510`. Public/private graph tests and docs use it. | **Retain** as currently observed primary traversal seam; S3 may change implementation owner, not behavior. | High.
| `load_graph_state` | LanceDB projection loader at `lumio_lancedb/graph.py:421-505`; root exported in `lumio_lancedb/__init__.py:16-23,61`. | No production caller in either repo found. Heavy public tests: `test_graph_tables.py:264-474`, ontology parity, S3 compatibility. No private match. | MessagePack/in-memory graph state is documented fallback; exact injection owner unresolved. **Unresolved**; do not remove solely because use is tests-only—the projection capability and parity are accepted. | High local, external unknown; S3/projection architecture blocks deletion.
| `DocumentSourceProcessor` | Class at `source_processor.py:457`; root exported `__init__.py:391,481`. | Public source use only definition; public tests `tests/test_source_processor.py:59,72`. **Private production** imports/constructs it in `lumio/ingest.py:158-162`; private shim re-exports it in `lumio/source_processor.py:11-21`. | New public `select_source_processor`/layered processors are the likely owner, but private `_CONVERTERS` seam deliberately preserves behavior. **Migrate**, then **remove after migration** only after private routing is moved and compatibility shim consumers are inventoried. | High; active private consumer conclusively blocks removal.
| `cosine_similarity` | Non-root helper in `embeddings.py:87-105`. | No public production call found. **Private production calls** in `chat_history.py:39,1205,1242`; public/private tests may cover embedding behavior. | No observed alternative with identical behavior. **Retain** (or migrate private recall to a selected existing scorer first). | High; active private consumer blocks removal.
| `is_semantic_index_stale` | Non-root helper `embeddings.py:144-147`. | No production match in either repo; no direct test match found in scoped search. | Adapter index freshness may own semantic state, but no evidenced drop-in replacement established. **Unresolved**. | Medium-high local; external module-import consumers unknown.
| `load_lancedb_adapter` | Dynamic optional composition helper `composition.py:42-51`; module `__all__` at `:60-66`, not root package export. | Public CLI imports/calls it at `cli.py:3687,3735`; eval `eval/test_retrieval_eval_gate.py:119`; example `examples/.../eval_semantic_st.py:103`. No private match. | Same module already offers `require_lancedb` and `load_lancedb_module` (`composition.py:25-59`), while app has its own configured factory. **Migrate** public caller to chosen composition function; **remove after migration** only after preserving optional failure semantics. | High. It is not dead.
| Activity Log trio | `_format_activity_log_line`, `make_activity_log_entry`, `append_activity_log_entry` at `knowledge_base.py:5373-5424`; latter two root exported `__init__.py:111,123,679,706`; `ActivityLogEntry` root exported too. | Active public producer calls in `proposal_pipeline.py:1980-2019`; **active private producer** imported `app.py:19-41` and called `app.py:1997-2005`. Private `tests/test_compound_ingest.py` and `tests/test_external_import_publish.py` also directly import/call the producer APIs; `tests/test_publish.py` asserts the app writer. Public tests assert writer behavior, and `scripts/verify_lumio_wiki_wheel.py:110-115` calls both exported producers from an installed wheel. Docs/history mention names. | ADR-0022 replacement is Published Version/Git/S3 history, not another portable writer. **Migrate** both production paths, direct private test consumers, and the wheel verifier to no write, then **remove after migration** producer APIs/record/export. Retain legacy recognition/collision/fingerprint exclusions (`knowledge_base.py`, `okf.py:1031`, `publish.py:165`). | High. Plan S1 now records the cross-repository lane, but the active private writer and direct test consumers remain unmigrated and further private mutation is not authorized.
| Root package exports | `lumio_wiki/__init__.py:3-391` imports a very broad surface and `__all__` spans `:404-755`; candidate aliases/constants explicitly included. `lumio_lancedb/__init__.py:16-67` exports tables, builders, searches, adapter, graph loader. | Private app has many explicit root imports (`app.py:19-49`, `ui.py:21-27`, `cli.py:7`, `gateway.py:34`, etc.) **and wildcard re-export** `lumio/core/__init__.py:7-8`, which dynamically exposes the entire `lumio_wiki.__all__` through `lumio.core`. `core/index.py:19-24` wildcard re-exports `lumio_lancedb.index`. | Narrowing must first remove/replace wildcard compatibility shims and inventory their actual callers. Candidate-by-candidate migration; overall action **unresolved**. | High that all root exports are runtime-reachable through private compatibility shim; impossible to prove which are accessed by external/private consumers through static name search alone.
| Snapshot/SDK traversal duplication | `KnowledgeBaseSnapshot` stores `KnowledgeBase` and delegates lookup/search/retrieve/traversal (`location.py:134-331`); `FilesystemLocation.resolve` loads once and captures fingerprint (`:343-381`); opening seam `:383-405`. `KnowledgeBase._knowledge_index` caches one index (`knowledge_base.py:445`), but `_load_and_validate` separately constructs `_KnowledgeIndex(pages)` for cross-page validation at `:4696-4708`. | Both façades are public/root-exported (`__init__.py:280-287`). Private production imports `KnowledgeBaseSnapshot` in `storage/s3.py:28` and `KnowledgeBase` widely; runtime calls KB traversal. | S3’s observed target is retain the validation-built index and have Snapshot delegate one bounded owner. **Retain** Snapshot and bounded behavior; migrate duplicate `graph_path` and redundant index derivation internally only after R1 identity/security gates. | High for duplicated derivation/delegation; performance impact unmeasured.
| Low-level constants/display helpers (B20/B21/C02/C04) | Root exports include graph artifact constants, control/version/mode constants, graph scopes/samples/weights, legacy Activity constants, and lifecycle display helpers (`__init__.py:27-29,66-99,417-755`). | **Private production actively consumes** `CONTROL_FILE_BASENAME`, `CONTROL_FILE_VERSION`, `KB_MODE_CATEGORIZED` (`app.py:20-22,1070-1096`); `DEFAULT_GRAPH_MAX_EDGES`, `GRAPH_SCOPE_DISCOVERY` (`constellation.py:34-51,233`); graph scopes (`runtime.py:12-13,939`); lifecycle display helpers (`ui.py:21-26,4156-4491`); `DERIVED_DIR`, `GRAPH_ARTIFACT_FILENAME`, `MANIFEST_OBJECT` from `s3_location` (`storage/s3.py:228-231,483-508`); string-qualified scope references (`skills/_authz.py:72-93`). Public LanceDB consumes `EXTRACTOR_VERSION` and graph artifact version (`lumio_lancedb/graph.py:33-36,485-487`). | **Retain** all active names pending migration to higher-level descriptors/snapshot APIs. Legacy Activity constants can remove only producer-facing exports while internal legacy recognition remains. Samples/weights with no app match remain **unresolved**, not proven dead. | High; dynamic string and wildcard references materially broaden risk.

### Private application production import inventory

All source modules with imports from either public distribution (grouped; exact import starts):

- root/API clients: `app.py:19-57`, `ui.py:21-28`, `cli.py:7,102`, `gateway.py:34-35`, `ledger.py:33-34`, `publish.py:23-32`, `storage/backends.py:13`, `storage/git.py:11`, `storage/shared.py:9`;
- retrieval/runtime/graph: `retrieval.py:24-32,338`, `runtime.py:7-16`, `constellation.py:34`, `chat_history.py:39-41`, `recall_index.py:23`;
- ingest/publish adapters: `ingest.py:39-91,158,179,247,446,608`, `proposal_pipeline.py:11`, `source_processor.py:11`, `distiller.py:15-19`;
- storage/S3 runtime imports: `storage/s3.py:28-29,107-108,158,228,241,301-302,483,506,516`;
- record/provider/UI helpers: `control_file_proposals.py:25`, `conversation_sources.py:24`, `qa_dashboard.py:21`, providers at `providers/__init__.py:5`, `fake.py:7-8`, `embeddings.py:18`, `openai_provider.py:7`, `sentence_transformers.py:21-22`;
- skills: `skills/cross_linker.py:7-10`, `skills/lint.py:7-8`;
- compatibility/dynamic surface: `core/__init__.py:7-8`, `core/index.py:19-24`, plus owner module shims `core/{embeddings,evidence,fingerprint_store,knowledge_base,okf,page_search,records,retrieval}.py:5`.

This is the complete scoped production-file import-site inventory at the stated private revision. Tests add extensive direct imports but are not production consumers.

### Package versions, ranges, adapters, entry points

- Public `lumio-wiki` is `0.1.2`, Python `>=3.14`, base dependencies only msgpack/msgspec; CLI entry `lumio-wiki = lumio_wiki.cli:main`; optional `documents`, `llm`, `s3`, `all` (`packages/lumio-wiki/pyproject.toml:1-38`).
- Public `lumio-lancedb` is `0.1.2`, depends `lumio-wiki>=0.1.2,<0.2.0`, LanceDB/PyArrow/msgspec, with `embeddings` and `s3` extras (`packages/lumio-lancedb/pyproject.toml:1-32`).
- Private `lumio` is `0.1.2`, pins both wheels `>=0.1.2,<0.2.0`; its `s3` extra pins `lumio-wiki[s3]` to the same range; CLI entry `lumio = lumio.cli:main` (`packages/lumio/pyproject.toml:1-43`). Root private `pyproject.toml:15-16` currently resolves both to editable sibling paths for development, consistent with ADR-0025’s temporary path-source allowance.
- Primary adapter entry is `LanceDBRetrievalAdapter` exported at `lumio_lancedb/__init__.py:24-37,48-66`; public composition discovers it dynamically (`composition.py:20-51`). Private app chooses it lazily in `retrieval.py:338` and retains deprecated adapter shim `core/index.py:1-24`.

## Architecture

The public `lumio-wiki` wheel owns durable Markdown loading, validation, ingest/publication, snapshots, and bounded traversal. `lumio-lancedb` depends inward on its records/fingerprint contract and provides disposable search/graph projections. The private app depends on both wheels, but still has broad direct submodule imports, wildcard compatibility façades, duplicate ingestion converter routing, direct low-level storage constants, and an independent Activity Log producer. Consequently, a root-export contraction is a coordinated migration, not a public-repo cleanup.

Traversal currently has two algorithms (`graph_path` legacy and bounded `shortest_path`) and two façades (`KnowledgeBase`, `KnowledgeBaseSnapshot`). Snapshot delegation itself is useful identity-preserving API; the concrete duplication is legacy BFS plus rebuilding `_KnowledgeIndex(pages)` during validation even though `KnowledgeBase` later caches another index. LanceDB `load_graph_state` reconstructs a source-fingerprint-matched in-memory `GraphState`; tests establish parity, but production injection is not observed in these revisions.

## Proposed migration ledger

| Order | Consumer revision/action required | Verification |
|---|---|---|
| 1 | Private app: stop `app.py` Activity Log write/imports while preserving atomic publish and private audit/version records. Migrate direct private producer-API tests to assert navigation/publication behavior without creating `log.md`. Public foundation: remove calls in `proposal_pipeline.py`, migrate the installed-wheel verifier, and retain legacy marked `log.md` recognition and collision rejection. | Public Plan S1 focused tests and isolated-wheel verification plus private `tests/test_publish.py`, `tests/test_page_removal.py`, `tests/test_proposal_pipeline.py`, `tests/test_compound_ingest.py`, `tests/test_external_import_publish.py`, `tests/test_okf_complete_journey.py`, and `tests/test_audit_log.py`; grep all three producer symbols in both repos and the wheel verifier.
| 2 | Private app: migrate `_parse_frontmatter` and `DocumentSourceProcessor`/local converter routing to the approved public ingest/composition owner; preserve converter provenance and optional dependency isolation. | Private `tests/test_ingest.py`, `test_ingest_surface.py`, `test_ingest_dependency_isolation.py`, `test_create_proposal_composition.py`, `test_conversation_source_processors.py`; public source/ingest journey tests.
| 3 | Retain `cosine_similarity` until private chat-history recall has an explicit owner/replacement. | Private `tests/test_chat_history.py`, recall/semantic tests.
| 4 | Migrate legacy `graph_path` test consumers and any confirmed external consumers to bounded `shortest_path` with explicit decisions for alias, authorization, bounds, and same/missing endpoints. Keep private runtime’s current bounded call. | Public `tests/test_graph_traversal.py`; private `tests/test_core_sdk.py`, `test_core_without_lancedb.py`, `test_graph_retrieval.py`, constellation/runtime tests.
| 5 | Consolidate optional adapter discovery (`load_lancedb_adapter`) into one public composition owner; retain separate app factory if its configuration/DI semantics differ. | Public eval gate, CLI remote Lance test, adapter selection tests; private `tests/test_adapter_selection_contract.py`, `test_remote_retrieval_seam.py`.
| 6 | Only after explicit compatibility inventory, remove private wildcard shims and narrow root exports/constants one name at a time. | Private `tests/test_core_compatibility.py`, `test_import_entrypoints.py`, `test_package_contraction.py`; installed-wheel scripts.
| 7 | Decide whether `load_graph_state` remains supported projection API or becomes internal injection. No removal without external inventory and parity/fallback evidence. | Graph tables, ontology parity, remote S3 compatibility tests.

## Removal gates / blockers

1. Inventory acceptance is recorded. Explicit implementation, private-repository mutation, API-removal, and release approvals remain absent; current Plan 05 grants none of them.
2. Obtain migration commits and passing commands from the private-app owner. The active private Activity Log and `DocumentSourceProcessor`/parser/cosine callers are hard blockers.
3. Inventory any additional owner-confirmed consumers. Static absence in these two repos is not external proof.
4. Resolve semantics before replacing `graph_path`; never weaken visibility-before-expansion, empty-seed non-broadening, cycle safety, depth/edge budgets, or fingerprint identity.
5. Preserve optional documents/providers/S3/LanceDB and raw-source/provenance behavior (C04); no deletion justified by base-install isolation.
6. Remove wildcard compatibility shims or prove their consumers before root `__all__` contraction.
7. Keep ADR-0022 legacy marked-log recognition and malformed/unmarked collision blocking even after all writers are removed.
8. Do not claim a graph performance benefit without measurement; audit showed duplication only.

## S4 validation drift and reconciliation

At public commit `4948aca`, five live-MinIO paths in Plan S4 no longer exist;
`tests/test_onboarding_journey.py` is the only listed path that remains current.
The provider-neutral replacements are the seven `*_s3_compat.py` live modules
under `packages/lumio-wiki/tests/` and `packages/lumio-lancedb/tests/`, including
the base S3-location and publication suites omitted by the old command.

Only stale compiled `__pycache__/*.pyc` names remain for some deleted MinIO
tests; they are generated and excluded. The documentation candidate captured
with this inventory updates Plan S4 to the pinned SeaweedFS environment, the
onboarding journey, and all seven current live modules. It also records that
each module must execute and that only the separately provisioned cross-role
policy matrix may skip with a disclosed reason. This resolves the copyability
defect but does not authorize S4 or satisfy its consumer-migration and release
gates.

### Relevant verification commands

Public focused commands are listed in the **Verify** blocks for Plan S1-S3
(`docs/plans/05-simplification-release.md`). Public candidate certification
begins with:

```sh
cd /home/volker/coding/lumio-wiki
uv lock --check
uv run pytest -q -n 4
uv run python -m ruff check packages tests eval scripts
git diff --check
```

Private repository’s own documented checks (`AGENTS.md:15-19`, `README.md:381-384`, `.github/workflows/ci.yml:25-28`) are:

```sh
cd /home/volker/coding/lumio
uv lock --check
uv run pytest -q -n 4
uv run ruff check .
```

The private CI relocates a checkout of `lumio-wiki` beside the app before these checks, which is relevant consumer-adoption evidence. No tests were run for this read-only inventory; verification commands are recorded, not claimed passing.

## Conclusion

Plan 05 removals are **not ready**. Confirmed production consumers exist for Activity Log producers, `DocumentSourceProcessor`, `_parse_frontmatter`, `cosine_similarity`, `shortest_path`, multiple low-level constants/display helpers, and broad wildcard root exports. `load_lancedb_adapter` also has public production callers. `as_sources`, `graph_path`, `load_graph_state`, and `is_semantic_index_stale` lack observed production calls in these two revisions, but external-consumer uncertainty and accepted graph/optional-capability contracts prevent dead-API conclusions. The smallest safe path is migration in the ledger order, preserving legacy-log reads and bounded traversal, followed by evidence-backed contraction. The five stale MinIO filenames were a defect in the inspected base and are reconciled by the accompanying Plan 05 documentation change; S4 remains blocked by its consumer-migration and separate release-approval gates.

## Start Here

Open `packages/lumio/src/lumio/app.py:19-57,1980-2020` first: it is the decisive cross-repository evidence that ADR-0022 retirement cannot be completed solely in the public files currently named by Plan S1.
