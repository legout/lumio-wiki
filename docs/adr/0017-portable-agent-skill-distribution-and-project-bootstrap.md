---
status: proposed
---

# ADR-0017: Portable Agent Skill Distribution and Project Bootstrap

## Context

ADR-0012 established that Agent Skill Markdown is a durable, versioned contract
that must ship with the code it describes. The `lumio-wiki` precedent already
stores `SKILL.md` and `PROTOCOL.md` as wheel data resolved through
`importlib.resources`. This prevents an external skill registry from silently
drifting away from the enforcing Python implementation.

The real-world coding-agent trial tracked by GitHub issue #147 proved the core
workflow but exposed a bootstrap and distribution gap:

- the installed skill told the agent to run `lumio-wiki init`, so the agent did
  not run `setup` and no project `.env` or `AGENTS.md` was created;
- `setup` subsequently wrote a valid `.env`, but a fresh CLI subprocess did not
  load it, despite CLI output and usage documentation promising pathless
  commands;
- the skill was already present in Pi's user-specific directory, while no
  project-local or cross-client skill was installed;
- copied skills carry no wheel version or content hash, so a package upgrade can
  leave agents using an indefinitely stale contract; and
- setup, skill, protocol, generated `AGENTS.md`, and usage documentation repeat
  overlapping instructions and have already diverged.

The [Agent Skills specification](https://agentskills.io/specification) defines
the portable directory and `SKILL.md` format, but deliberately does not mandate
installation paths. Its [client implementation guide](https://agentskills.io/client-implementation/adding-skills-support)
identifies user/project `.agents/skills/` directories as the cross-client
interoperability convention while allowing client-specific locations. Pi also
discovers `.agents/skills/` at both user and trusted-project scope.

Project bootstrap and skill installation have different responsibilities. A
project must record which Knowledge Base it uses and the guardrails every agent
must follow. The generic Lumio Wiki capability should not need to be copied
anew into every project, and installing instruction-bearing or executable skill
content must remain an explicit user action.

## Decision

### Keep the wheel as the canonical skill contract

The `lumio-wiki` wheel remains the sole canonical distribution source for the
Agent Skill and its referenced protocol/resources. Skill content is committed,
reviewed, tested, and released with the Python behavior it describes. No
separate marketplace, registry, gist, or manually maintained repository copy
becomes authoritative.

The skill follows the Agent Skills directory contract and progressive-
disclosure model:

- `SKILL.md` contains the trigger description and concise operating workflow;
- detailed protocol material is referenced by a relative path under the skill
  directory; and
- scripts, references, and assets are loaded only when needed.

Standards-compatible metadata records the Lumio Wiki distribution version. CI
validates the built-wheel copy, not only source-tree Markdown.

### Make `setup` the canonical project bootstrap

`lumio-wiki setup <kb-path>` is the documented first-run command. `init`
remains the lower-level operation that creates only a Knowledge Base directory.
Unless explicitly disabled, setup:

1. creates or recognizes the Knowledge Base;
2. writes/updates project `.env` with `LUMIO_KB_PATH`; and
3. writes/updates a compact project `AGENTS.md` section containing the KB-local
   retrieval ladder, citation/refusal rules, proposal-first workflow, and
   maintenance commands.

The CLI resolves a missing positional Knowledge Base path in this precedence
order:

1. explicit positional path;
2. exported process `LUMIO_KB_PATH`;
3. `LUMIO_KB_PATH` from the nearest project `.env` from the current working
   directory up to the trusted project root; and
4. an actionable missing-path error.

An exported value is never overwritten by `.env`. The CLI reads only the Lumio
KB path required for this behavior rather than importing arbitrary project
environment variables. Relative `.env` values resolve against the directory
containing that file. Fresh-subprocess integration tests, not in-process
`monkeypatch.setenv` simulations, own this contract.

`AGENTS.md` is the project-specific cross-agent handoff. It records where the
Knowledge Base is and the minimum durable behavior a restarted or different
agent needs. It does not duplicate the full generic skill implementation.

### Support shared and client-specific skill installation scopes

Skill installation remains explicit. Package installation and ordinary setup
never silently write into an agent's instruction directories.

The CLI supports these shared Agent Skills destinations:

- user scope: `~/.agents/skills/lumio-wiki/`;
- project scope: `<project>/.agents/skills/lumio-wiki/`.

User scope is the preferred reusable cross-client installation. Project scope
is opt-in for repositories that intentionally want the skill to travel with a
trusted checkout. Client-specific destinations for Pi, Hermes, Codex, and
Claude Code remain explicit compatibility targets for clients that do not scan
the shared convention.

Setup may combine project bootstrap with a requested skill installation, but it
must distinguish the two actions in output and require an explicit scope or
agent target. It tells users that a newly installed skill is discovered when
the agent starts or restarts.

### Make copied skills inspectable and updateable

Every installed copy carries a small manifest containing at least:

- distribution name and version;
- content hash of the installed skill bundle;
- installation scope/target; and
- source wheel contract hash.

`lumio-wiki skill status` is read-only and reports missing, current, stale, or
corrupt. `lumio-wiki skill update` performs an explicit atomic refresh from the
installed wheel. An existing destination is not treated as current merely
because `SKILL.md` exists. Partial copies remain impossible.

### Prevent protocol drift

The command/workflow facts repeated across `SKILL.md`, the detailed protocol,
generated `AGENTS.md`, CLI help, and usage documentation are either rendered
from one structured source or protected by parity tests at the installed-wheel
surface. In particular, all surfaces distinguish:

- `setup` from lower-level `init`;
- authored Markdown links/Extracted References from reviewed typed
  Relationships;
- project bootstrap from generic skill installation; and
- raw Knowledge Source provenance from agent-authored Compiled Page content.

## Considered Options

- **Wheel data only, with no installation command** — rejected because coding
  agents do not generally discover Python package resources as skills without a
  bootstrap path.
- **Vendor-specific user directories only** — rejected as the default because
  it duplicates the same skill across clients and weakens cross-client reuse;
  retained as an explicit compatibility fallback.
- **Always vendor the full skill into every project** — rejected because copied
  contracts drift, repositories may be untrusted, and the generic capability is
  not project-specific. Project scope remains an explicit option.
- **Install or update skills automatically during `pip`/`uv` installation** —
  rejected because skills can contain executable instructions and installation
  into agent trust surfaces must be visible and intentional.
- **Use an external skill marketplace as the canonical source** — rejected by
  ADR-0012's versioning and no-drift rationale. Generated release bundles may be
  offered for convenience, but the wheel contract remains authoritative.
- **Rely on shells or agent harnesses to load `.env`** — rejected because shell
  variables may be unexported, harness behavior differs, and setup explicitly
  promises that the CLI can resolve the project Knowledge Base afterward.

## Consequences

Fresh and restarted coding-agent sessions gain two independent guarantees:
project `AGENTS.md` supplies KB-specific behavior, and an optional installed
skill supplies the richer reusable Lumio Wiki workflow. Users of several
Agent Skills clients can share one user-scope installation where supported.

The CLI gains bounded project `.env` discovery, skill manifests, status/update
operations, shared-scope destinations, and fresh-process certification tests.
Existing vendor-specific install commands remain compatible, but old copied
skills report stale until explicitly refreshed.

ADR-0012 remains the authority for committing and wheel-packaging skill
contracts; this ADR extends it with installation, update, and project-bootstrap
semantics. ADR-0015 remains the authority for the portable Maintainer workflows
written into `AGENTS.md`.

Implementation is tracked by #147 and its child issues: #152 (environment
loading), #148 (first-run protocol), #149 (raw-source-preserving host
Distillation), #151 (typed Relationship CLI), and #150 (skill distribution and
updates).
