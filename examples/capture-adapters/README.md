# Capture client adapters (opt-in, outside the canonical SDK)

Issue #179's vendor-neutral capture core accepts any client: a capture is an
agent-authored Compiled Page plus a bounded manifest, staged through ONE
public contract:

```bash
lumio-wiki capture session <kb> --compiled-page <page.md> \
  --manifest <capture.yaml> --source-id <id> [--transcript <path>] [--yes]
```

The adapters below are thin, opt-in export recipes — where each client keeps
its session transcript, and what to put in the manifest. They deliberately
live outside `packages/lumio-wiki` (the canonical SDK stays client-agnostic);
no adapter code is required to use capture.

## Manifest fields (vendor-neutral)

```yaml
client: pi                    # pi | codex | claude-code | hermes | any manual label
project: lumio-reader
started_at: "2026-08-24T09:00:00Z"
ended_at: "2026-08-24T10:30:00Z"
transcript: pi-session.jsonl  # local path, or "sha256:<64-hex>" digest reference
artifacts:                    # names of artifacts included in the capture
  - search-results.md
redactions:                   # labels for exclusions you already applied
  - oracle-a1 host IP
```

## Pi

- Sessions live under `~/.pi/agent/sessions/` (JSONL rollups; pick the
  current session's file).
- Export: copy the session JSONL next to your manifest and point
  `transcript:` at it. When you cannot read it, reference its SHA-256:
  `transcript: "sha256:<digest-of-the-file>"` — capture then reports the
  limitation and registers the manifest as the private record.
- Author the page from decisions/verified findings/commands/citations.

## Codex

- Sessions live under `~/.codex/sessions/` (rollout JSONL files).
- Export: copy the rollout file as `transcript:`. Rollout files contain
  chain-of-thought blocks; the core strips `<think>`/`<reasoning>` blocks
  during redaction, and your page must be declarative regardless.

## Claude Code

- Session transcripts are JSONL files under `~/.claude/projects/<project>/`.
- Export: copy the project transcript JSONL as `transcript:`.
- The turn structure is nested; capture only needs the raw bytes — the page
  you author is what stages.

## Hermes

- Memories/sessions live under `~/.hermes/` (memory snapshots, session logs).
- Export: the session log or memory snapshot file as `transcript:`.

## Unsupported clients (manual manifest)

Any client can capture through a manual manifest: use any lowercase
`client:` label (or `manual`), leave `transcript:` null when no export
exists, and run the same command. The manifest itself becomes the private
capture record registered under your source id; the preview notes that no
transcript was bound.

## Rules every adapter inherits

- Explicit consent only — never capture in the background.
- Preview first: without `--yes` nothing is registered or staged.
- Secrets, credentials, signed URLs, private object keys, environment
  dumps, and hidden reasoning are redacted before staging and never reach
  the page, the manifest record, logs, or output.
- A missing or unreadable named transcript is an error, not a gap to fill:
  report the limitation (digest-only manifest) instead of fabricating.
- Capture never auto-publishes; review and publish stay explicit.
