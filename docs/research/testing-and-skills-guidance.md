# Testing and skills guidance for keeping implementations small

_Date: 2026-09-11. Question (user wording): "avoid long implementation due to
testing how? are there skills? ANGETS.md instructions?" — i.e. how to minimize
implementation scope while satisfying the repo's testing obligations, and which
skills / AGENTS.md rules govern that work. Research only; no files were edited._

_2026-09-14 correction: the framework now assigns obligations to validation
units, uses scoped authority rather than linear source precedence, and defaults
to lean assurance. The original observations below are updated accordingly._

## Findings

### 1. AGENTS.md is the binding testing entry point

- Focused tests run **serially** while developing: `uv run pytest -q <test paths>`;
  the full suite uses a **fixed four workers**: `uv run pytest -q -n 4`. `-n auto`
  is forbidden (`AGENTS.md`, "Validation").
- Each validation unit receives exactly one obligation — `new-test`,
  `existing-check`, or `no-new-test`. Related tasks may share a unit, and focused
  TDD applies only to `new-test`. The label follows the actual reachable failure;
  it is not a way to waive evidence.
- "Do not turn a trivial edit into a planning exercise": trivial edits skip
  `shape-design`, `write-implementation-plan`, and `orchestrate-implementation`.
- Authority is scoped, not linear: glossaries own terminology, ADRs own accepted
  architectural constraints, PRDs own behavior, and plans/issues own execution
  decomposition. Conflicts are reconciled in their owning artifacts before work.
- Lean assurance adds no test, command, reviewer, or review round unless it covers
  a distinct named failure mode. Normal work gets one candidate review; low-risk
  work uses parent diff inspection; high-risk or dependency-defining work also
  gets immediate review.

### 2. Core SDK PRD: smallness is an architectural property

- "Deep module, small seam" keeps the internal split private.
- **"The interface is the test surface"**: tests cross the same seam callers do;
  reaching past it signals the module may have the wrong shape.
- "One adapter means a hypothetical seam; two means a real one"; test-only
  internals are not promoted to public API.
- Work uses coherent vertical slices through load → validate → index → retrieve.
  Several related slices may share one validation unit; a new test is added only
  when a named failure lacks meaningful public-seam coverage.
- The required checks use representative fixtures and exclude model providers,
  network access, and the application framework from this seam.

### 3. CONTEXT.md: vocabulary keeps scope discussions precise

- `CONTEXT.md` defines the relevant terms: **Evidence** is rebuildable from
  Markdown, **Lint** is model-free and read-only, and **Write Mode** distinguishes
  proposal-first from direct write. Scope discussions use those canonical terms;
  tests prove behavior from specifications rather than treating glossary prose as
  an exact wording contract.
- The `lumio` skill reinforces: "use its exact vocabulary in specs/issues …
  Avoid-terms matter as much as the terms" (`~/.hermes/skills/software-development/lumio/SKILL.md`,
  "Read before acting").

### 4. ADRs on skills and agent workflows

- **ADR-0012** (`docs/adr/0012-commit-agent-skills-to-repo-as-versioned-contract.md`):
  skills are durable contracts, not orchestration artifacts; each skill is thin —
  "it delegates" to public `lumio_wiki` operations (lines 9-13, 40-47, 75). Skill
  write policy and role gates "remain enforceable in tests **without the web
  stack**" (lines 89-92) — small, framework-free tests are the established norm.
- **ADR-0017** (`docs/adr/0017-portable-agent-skill-distribution-and-project-bootstrap.md`):
  skill content is committed and released with the behavior it describes.
  Operationally critical repetition comes from one source where practical;
  otherwise one focused installed-wheel journey protects each executable or
  safety contract. Exhaustive wording parity is not required.
- **ADR-0024** (`docs/adr/0024-graph-exchange-and-cli-skill-boundary.md`): the CLI
  skill boundary exists because per-agent prose transforms were untestable and
  divergent. Focused semantic checks replace byte-identical gold-file mandates;
  the no-new-dependency decision remains.
- **ADR-0010** (`docs/adr/0010-uv-workspace-and-progressive-packaging.md`): CI must
  build each wheel and test it in isolation, including "testing each optional
  ingestion extra independently" and the CLI + packaged skills "from the built
  wheel" (lines 169-179); the workspace exists to stay a "buildable, independently
  testable headless product" (line 192).
- **ADR-0025** (`docs/adr/0025-repository-split.md`): the public repo carries the
  wiki-only test suite plus each package's own tests; application-level tests
  stayed private (lines 24-52) — test scope follows package boundaries.

### 5. Research-note conventions (what this file follows)

Existing notes under `docs/research/` share a shape: a title, a metadata block
(`_Date: YYYY-MM-DD. Scope: …_`), then `## Findings` bullets citing primary
sources — see `docs/research/lancedb-s3-remote-storage.md` (head) and
`docs/research/anydoc-fit.md` (Tracking + Verdict sections linking a GitHub
issue). This note adopts that shape.

### 6. Relevant installed Hermes skills

Verified present under `~/.hermes/skills/software-development/` (directory
listing, 2026-09-11):

Directly on-point for implementation/testing in this repo:

- `lumio` — repo conventions, including: exercise real registry/pipeline
  transitions not mocked state; CLI tests via `main([...])` + `capsys`; hashes
  via `artifact_content_hash(bytes)`, never hardcoded digests; advisory
  diagnostics never block publish ("Conventions" section).
- `test-driven-development` — RED-GREEN-REFACTOR; the focused-TDD discipline
  AGENTS.md requires for `new-test` tasks.
- `verification-before-completion` — matches AGENTS.md's requirement to use it
  "before success claims".
- `writing-plans` / `plan` — for approved multi-step work (AGENTS.md routing).
- `simplify-code`, `delegated-work-verification`, `debugging`, `spike` —
  smallness, verification of delegated work, and throwaway validation.

Adjacent (named by AGENTS.md or repo workflow, present as skills): `github`
(issue tracker is GitHub Issues per `docs/agents/issue-tracker.md`),
`grill-for-unknowns`, `evidence-driven-repo-simplification`, `brainstorming`.

Note: the orchestrator block in AGENTS.md also references repo-side workflows
(`shape-design`, `write-implementation-plan`, `orchestrate-implementation`,
`pi-subagents`, `merge-worktree`, `make-release`) that are **not** installed as
Hermes skills in this profile — they are conventions of the Pi orchestrator, not
skill files here. Ambiguity flag: the user's "are there skills?" could mean
either; both readings are covered above.

## Ambiguities in the request

- "ANGETS.md" is read as a typo for **AGENTS.md** (its content was provided in
  the task context and matches the file at the repo root).
- "avoid long implementation due to testing" is interpreted as _how to keep
  implementation scope minimal while still meeting assurance obligations_ —
  via shared validation units, public-seam checks, and vertical slices. If the
  user instead meant test runtime, the answer is the serial focused-run rule
  plus one fixed-`-n 4` candidate run (`AGENTS.md`, "Validation").
