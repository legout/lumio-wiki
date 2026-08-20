---
status: proposed
amends: ADR-0013, ADR-0017
---

# ADR-0019: Coding-agent setup and complete S3 publication

## Context

Lumio already has the pieces needed for an S3-native coding-agent workflow, but
not one coherent operating interface. `lumio-wiki setup` bootstraps only a
local Knowledge Base; direct S3 reads use zero-index retrieval; remote LanceDB
is available only through lower-level Python interfaces; and `publish-s3`
activates canonical Markdown plus the Discovery Graph without first building an
optional remote LanceDB index. A user must currently understand package extras,
object-key layouts, environment precedence, and adapter construction to assemble
what should be one setup choice.

An S3 Knowledge Base Location resolves immutable Published Versions. It is not
a mutable authoring filesystem. Maintainers therefore still need an explicit
local worktree for ingest, proposal review, and publication, while read-only
coding agents may consume the active S3 Published Version directly.

## Decision

### Extend `setup`; do not introduce a second configuration file

`lumio-wiki setup` remains the canonical project bootstrap. It gains two
role-oriented forms:

```text
lumio-wiki setup <local-kb> --publish-to <s3-uri> [--retrieval zero-index|lancedb]
lumio-wiki setup --from <s3-uri> [--retrieval zero-index|lancedb]
```

The first form creates or recognizes a local Maintainer worktree and records an
S3 publication destination. The second configures a read-only project against
an existing S3 Knowledge Base Location. Existing explicit skill targets remain
available through `--skill-scope user|project` and `--agent <name>`.

With no location arguments in an interactive terminal, `setup` may ask the same
questions as a short wizard. In a non-interactive process it fails with the
required flags instead of waiting for input. Flags and wizard answers produce
the same configuration and are certified by the same journey tests.

Project configuration stays in the `.env` already established by ADR-0017. The
CLI reads a bounded Lumio allowlist rather than importing arbitrary environment
variables. At minimum the configuration distinguishes:

- the local Maintainer worktree (`LUMIO_KB_PATH`) or read-only S3 Location;
- the optional S3 publication destination;
- retrieval backend and retrieval mode; and
- optional S3 region and compatible endpoint settings.

`lumio.yaml` remains portable Knowledge Base content and never contains object-
store credentials or deployment locations. Setup never writes credentials.
Standard AWS credential resolution remains preferred; explicitly supplied
Lumio/AWS environment credentials remain deployment configuration.

Setup does not mutate its own Python environment. It detects missing
`lumio-wiki[s3]` or `lumio-lancedb[s3]` capabilities and prints one exact
`uv`/`pip` installation command. Skill installation remains an explicit setup
flag because an Agent Skill is a trust surface, not an ordinary dependency.

### Bind enhanced retrieval to the resolved S3 Snapshot

The standalone CLI resolves the active Published Version once. When LanceDB is
configured, the resolved Snapshot exposes a dependency-neutral descriptor for
that exact version's `derived/lance/` location and fingerprint. The CLI hands
that descriptor to `lumio-lancedb`; it does not reconstruct object keys in each
caller or coerce an S3 URI into a filesystem path.

Retrieval backend and retrieval mode are separate choices: zero-index lexical
retrieval remains always available; LanceDB may provide BM25 lexical, semantic,
or hybrid retrieval. A missing, unavailable, corrupt, or fingerprint-mismatched
remote index falls back to zero-index retrieval over the same Snapshot and says
so in the Retrieval Trace.

### Prepare, validate, then activate one complete Published Version

S3 publication becomes one prepare/build/validate/activate operation. Before
expensive work starts, the publisher reads the active pointer and captures the
compare-and-swap intent. It then writes into a previously absent immutable
version prefix, validates canonical Markdown and the Discovery Graph, builds and
health-checks the requested LanceDB index, writes completion metadata, and only
then conditionally advances `current.json`.

If LanceDB was requested, index failure blocks activation. If it was not
requested, its absence is valid. Interrupted or failed preparation may leave an
unreferenced version prefix, but Readers remain on the previous valid Snapshot.
A cleanup operation may remove inactive incomplete prefixes.

Rollback is an explicit compare-and-swap activation of an already complete,
validated immutable version. It never rebuilds or overwrites that version.

## Considered Options

- **Add `.lumio/config.yaml`** — rejected for now. The established project
  `.env` can represent the small deployment allowlist without adding another
  precedence system. A structured config file can be introduced if the
  configuration outgrows environment-shaped values.
- **Make S3 a mutable Knowledge Base filesystem** — rejected. It would discard
  immutable publication, atomic pointer activation, and consistent Reader
  Snapshots.
- **Build LanceDB after advancing `current.json`** — rejected. Readers could
  observe a Published Version before its requested retrieval artifacts were
  complete.
- **Automatically install missing distributions from `setup`** — rejected.
  Modifying an unknown pip/uv/tool environment is unreliable and surprising;
  setup instead provides an exact command.
- **Put credentials in `lumio.yaml`** — rejected. It breaks portability and can
  leak deployment secrets through exports.

## Consequences

A new project can be configured for local Maintainer authoring or read-only S3
consumption with one command, and a restarted coding harness can use pathless
commands plus the installed skill. The CLI gains a single deep orchestration
seam for local/S3 location resolution, retrieval binding, and complete
publication rather than distributing S3 key and adapter knowledge across
commands.

Publication becomes more expensive when LanceDB is requested, but activation
remains atomic and truthful. Existing local, zero-index, and S3-without-LanceDB
workflows remain valid.
