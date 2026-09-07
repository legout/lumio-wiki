# Library stabilization and simplification

Status: **proposed implementation plans; not approved for execution**, except the test reduction explicitly authorized by the owner. Baseline: `9712f6fd51ac4381a6ca4fc513e2546355fba0d0`.

## Owner decisions

- Reduce tests to essential journeys plus distinct safety/validation regressions. Remove tests rather than hiding cases in loops or changing collection. Accept less incidental formatting, representation, export-inventory and noncritical edge coverage; no arbitrary test-count target.
- Preserve supported optional features. Fix and simplify them before adding the bounded agent improvements below.
- A coordinated breaking API release is allowed. Identify and migrate actual consumers, including the separate private Lumio app; do not keep indefinite compatibility shims.
- Local writes are serialized per KB, with rollback on ordinary failures. Crash-atomic publication and atomic visibility to concurrent readers are **not** promised.
- Disjoint staged proposals can publish sequentially. Use affected-file/control preconditions, not a whole-KB fingerprint that invalidates every outstanding proposal.
- Keep plans in this directory. No GitHub tickets, commits, pushes or implementation of the remaining findings are authorized by these plans alone.

## Sources and boundaries

[Core PRD](../prd/0002-core-sdk.md), [packaging ADR-0010](../adr/0010-uv-workspace-and-progressive-packaging.md), [ontology ADR-0021](../adr/0021-entity-claim-ontology-and-progressive-graph-materialization.md), [Activity Log retirement ADR-0022](../adr/0022-retire-activity-log-artifact.md), [exchange ADR-0024](../adr/0024-graph-exchange-and-cli-skill-boundaries.md), and [repository split ADR-0025](../adr/0025-repository-split.md) govern the work. ADRs 0019/0020 are marked proposed: their shipped behaviors/tests are evidence, not accepted architectural authority. The older PRD's ingestion exclusion is superseded by ADR-0010.

No private-app production behavior was audited. No real Knowledge Base may be used as a test fixture. Do not introduce a database, generic plugin/storage hierarchy, new agent runtime, mandatory provider, automatic Claim publication, or additional distribution.

The original audit ran the current workspace interpreter: **1,709 passed, 11 skipped**, approximately 89 seconds, and focused Ruff was clean. Targeted temporary repros nevertheless exposed the defects below. The local `.venv/bin/pytest` launcher points at an old `/tmp` interpreter; use `uv run python -m pytest`, not that launcher, until the environment is recreated separately.

## Plans and integration order

1. [Test retention and reduction](01-test-retention.md) — authorized now; establish the smaller test surface first.
2. [Publication and validation integrity](02-publication-integrity.md) — canonical validation, destination identity, locking, proposal preconditions, rollback.
3. [Snapshots and retrieval](03-snapshot-retrieval.md) — one captured source identity, S3 binding, derived-index lifecycle, useful passages and evaluation.
4. [Agent workflows](04-agent-workflows.md) — safety and command parity, then lossless bounded reads and current authoring guidance.
5. [Simplification and coordinated release](05-simplification-release.md) — retire obsolete production behavior, consolidate public/graph surfaces, migrate consumers.

Each task is independently checked. Keep one writer per cwd/worktree. Plans 2–4 may be developed in isolated worktrees after the test reduction, but their shared `knowledge_base.py`, `ingest.py`, `proposal_pipeline.py`, `cli.py`, `__init__.py` and test fixtures require **serial integration**. Integrate validation/identity before mutation preconditions; loaded-snapshot identity before S3/index work; agent fixes before schema/I/O additions; API removals last. No automatic merge or release.

## Finding-to-task map

Paths in this table are under `packages/lumio-wiki/src/lumio_wiki/` unless otherwise stated. Task IDs are stable handoff references.

| Finding | Baseline evidence | Plan task / acceptance |
|---|---|---|
| B01 New title slug overwrites another page | `publish.py:143–149` | P2: occupied/duplicate destinations rejected without mutation |
| B02 Lost SourceRegistry updates | `source_registry.py:269–302` | P3: separate instances/processes cannot lose successful writes |
| B03 Cached discarded proposal can publish | `ingest.py:1558–1587` | P3: durable terminal-state check under lock |
| B04 Stale reviewed base overwrites newer content | `proposal_pipeline.py:1185–1209` | P4: overlapping conflicts rejected; disjoint edits succeed |
| B05 Partial local publication after write failure | `proposal_pipeline.py:1205–1249` | P5: restore files/control/artifacts/status on ordinary failure |
| B06 Malformed Claims silently disappear; confidence crashes | `knowledge_base.py:2434–2538,2712` | P1: aggregate structural errors without dropping diagnostics |
| B07 Invalid literal structures/zero line anchors accepted | `knowledge_base.py:3128–3166` | P1: scalar kinds and 1-based coordinates enforced |
| B08 Old loaded pages stamped with current disk digest | `knowledge_base.py:1421–1425,1653–1660` | R1: one immutable content identity; no false freshness |
| B09 Lexical rebuild leaves old semantic evidence usable | `../lumio_lancedb/index.py:718–731` in adapter package | R2: invalidate vectors or reject mismatched source identity |
| B10 S3 content/fingerprint/bindings read different worktree states | `s3_publish.py:290–355`; `artifact_store.py:738–760` | R3: every artifact derives from one captured candidate |
| B11 Required-retention rollback accepts partial manifests | `artifact_store.py:771–805` | R4: verify every historical required page/source pair |
| B12 Nested headings yield duplicate whole-page evidence | `evidence.py:23–48` | R5: focused nested passages, meaningful snippets, bounded duplication |
| B13 JSON credential redaction misses quoted keys | `capture.py:95–98` | A1: quoted/escaped credential fixtures removed before preview/stage |
| B14 Slow URL headers exceed deadline | `url_fetch.py:262,299–305` | A2: connection/header/body deadline enforced |
| B15 AnyDoc ordinary CLI ingest crashes | `cli.py:1950`; `ingest.py:657–660` | A3: one preparation path, real CSV CLI proposal |
| B16 Citation replay switches KB | `citation_actions.py:186–198` | A4: explicit original location in every executable action |
| B17 Provider errors leak arbitrary exception text | `distiller.py:224`; `cli.py:6330+` | A1: safe CLI error without raw provider message/traceback |
| B18 Packaged commands/schema and README drift | `data/skill/SKILL.md:106,174+`; `README.md` | A5/A7: executed examples and valid v2 authoring |
| B19 Retired Activity Log still generated | `proposal_pipeline.py:1212–1240` | S1: remove producers, retain legacy recognition |
| B20 Root API inventory / duplicate traversal / repeated graph derivation | `__init__.py`; `knowledge_base.py:613,750,973,4427` | S2/S3: consumer-backed surface and one traversal owner |
| B21 Optional adapter discovery coupled to evaluation | `retrieval_eval.py:295–329`; `cli.py:375+` | A3/S2: one explicit composition helper, core stays dependency-light |
| I01 Lossless bounded agent reads and stable machine output | `cli.py:1546–1600` | A6 |
| I02 KB-aware authoring and simple local onboarding | `distiller.py:62–74`; `docs/quickstart.md` | A5/A7 |
| I03 Realistic negatives and passage/lifecycle evaluation | `eval/gold_set.yaml`, `eval/test_retrieval_eval_gate.py` | R6 |
| I04 Explicit source-content prompt-injection boundary | packaged protocol | A7 |

## Global verification

Run focused tasks serially, and the complete retained suite with four workers:

```sh
uv run python -m pytest -q <explicit task test files>
uv run python -m pytest -q -n 4
uv run python -m ruff check packages tests eval scripts
uv lock --check
git diff --check
```

For production/package changes, also run the existing installed-wheel gates in `.github/workflows/ci.yml` and `lumio-lancedb-wheel.yml`. Before builds, inspect LSP diagnostics for changed Python files. Live MinIO tests require an explicitly provisioned disposable service; report skips rather than claiming live S3 verification. Use existing tests/journeys whenever they fail for the intended change; a new bug gets one focused regression only when surviving coverage cannot distinguish it. Do not recreate the deleted microtest inventory.

Approval of these documents will authorize a later implementation phase only at the agreed scope. Each task needs fresh evidence; the audit's passing suite is not proof of a future fix.
