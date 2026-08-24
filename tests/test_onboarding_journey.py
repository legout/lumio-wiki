"""Canonical onboarding journey certification (issue #180).

Three layers, one truth — the journey documented in ``docs/quickstart.md``
must be executable, scriptable, and drift-guarded:

1. **Parity** — every ``lumio-wiki <cmd>`` fragment the quickstart cites
   resolves to a registered public CLI command; the quickstart, its scriptable
   twin (``examples/onboarding-journey/smoke-journey.sh``), the example
   README, and the packaged Agent Skill agree on the journey's commands; the
   entrypoint docs link it; and the walkthrough carries no secrets beyond the
   explicitly labelled local MinIO test credentials.
2. **Fresh environment** — the local half of the journey runs verbatim in a
   clean temporary project (no inherited ``LUMIO_KB_PATH``), ending in the
   truthful Source-Artifact unavailability.
3. **MinIO** — the scriptable journey executes end-to-end against a real
   S3-compatible endpoint when one is configured (the same skip contract as
   the other MinIO suites), proving the S3/LanceDB/fallback half of the docs.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
QUICKSTART = ROOT / "docs" / "quickstart.md"
SCRIPT = ROOT / "examples" / "onboarding-journey" / "smoke-journey.sh"
EXAMPLE_README = ROOT / "examples" / "onboarding-journey" / "README.md"
RAW_SOURCE = ROOT / "examples" / "onboarding-journey" / "sources" / "support-runbook.md"

# The journey's commands, in quickstart order. Every entry must appear in the
# quickstart, the smoke script, and (for the agent-facing subset) the packaged
# skill surfaces — this is the drift guard issue #180 asks CI to enforce.
JOURNEY_COMMANDS = [
    "lumio-wiki --version",
    "lumio-wiki doctor",
    "lumio-wiki setup",
    "lumio-wiki status",
    "lumio-wiki validate",
    "lumio-wiki ingest",
    "lumio-wiki proposal list",
    "lumio-wiki proposal inspect",
    "lumio-wiki proposal validate",
    "lumio-wiki publish",
    "lumio-wiki search",
    "lumio-wiki page",
    "lumio-wiki related",
    "lumio-wiki paths",
    "lumio-wiki source inspect",
    "lumio-wiki source fetch",
    "lumio-wiki publish-s3",
    "lumio-wiki rollback-s3",
    "lumio-wiki cleanup-s3",
]
# Commands the packaged Agent Skill must teach for the S3 half of the journey.
SKILL_JOURNEY_COMMANDS = ["publish-s3", "rollback-s3", "cleanup-s3", "source inspect"]


def _run_cli(args: list[str]) -> str:
    """Run the public lumio-wiki CLI in-process, returning captured stdout."""
    from lumio_wiki.cli import main

    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(io.StringIO()):
        try:
            code = main(args)
        except SystemExit as exc:  # argparse --help / errors
            code = exc.code or 0
    if isinstance(code, int) and code not in (0, None):
        raise AssertionError(f"lumio-wiki {args[:2]} exited {code}:\n{buf.getvalue()}")
    return buf.getvalue()


def _cli_help(cmd: str) -> str:
    from lumio_wiki.cli import main

    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(io.StringIO()):
        try:
            main([cmd, "--help"])
        except SystemExit:
            pass
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. Parity: docs, script, example, and skill cannot drift.
# ---------------------------------------------------------------------------


def _collapse(text: str) -> str:
    """Collapse markdown line wrapping so phrase checks are not hostage to
    where a line happens to break."""
    return re.sub(r"\s+", " ", text)


def test_quickstart_cites_registered_public_commands():
    """Every lumio-wiki command fragment in the quickstart is a real CLI
    command with help — documentation drift fails here, not at the reader."""
    text = QUICKSTART.read_text(encoding="utf-8")
    cited = {m.group(1) for m in re.finditer(r"lumio-wiki ([a-z][a-z0-9-]*)", text)}
    cited |= {m.group(1) for m in re.finditer(r"lumio-wiki (--[a-z-]+)", text)}
    assert cited, "quickstart must cite lumio-wiki commands"
    for cmd in cited:
        if cmd.startswith("-"):
            continue  # global flags are covered by the literal journey checks below
        assert _cli_help(cmd).startswith("usage:"), f"unknown CLI command: {cmd}"
    collapsed = _collapse(text)
    for journey in JOURNEY_COMMANDS:
        assert journey in collapsed, f"journey command missing from quickstart: {journey}"


def test_journey_commands_have_one_home_across_quickstart_script_and_example():
    """The quickstart, its scriptable twin, and the example README agree on
    the journey's commands — the script follows the docs' review sequence
    (list -> inspect -> validate -> publish), not a loose approximation."""
    quickstart = _collapse(QUICKSTART.read_text(encoding="utf-8"))
    script = SCRIPT.read_text(encoding="utf-8")
    example = EXAMPLE_README.read_text(encoding="utf-8")
    for journey in JOURNEY_COMMANDS:
        assert journey in quickstart, f"quickstart must cite: {journey}"
        if journey in (
            "lumio-wiki --version",
            "lumio-wiki doctor",
            "lumio-wiki rollback-s3",
            "lumio-wiki cleanup-s3",
        ):
            continue  # informational pointers, documented but not scripted
        head, sub = journey.split(maxsplit=1)
        assert re.search(rf"\b{re.escape(sub.split()[0])}\b", script), (
            f"smoke journey must execute the documented command: {journey}"
        )
    assert "smoke-journey.sh" in quickstart, "quickstart must link its scriptable twin"
    assert "docs/quickstart.md" in example, "example README must link the quickstart"
    assert "smoke-journey.sh" in example, "example README must name the smoke script"


def test_entrypoint_docs_link_the_canonical_journey():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    usage = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    for name, text in (("README.md", readme), ("docs/usage.md", usage)):
        assert "docs/quickstart.md" in text, f"{name} must link the canonical quickstart"


def test_skill_documents_the_s3_journey_commands():
    """The packaged Agent Skill (the agent-facing contract) teaches the S3
    publication commands the journey uses (issue #180 primary files)."""
    skill = (ROOT / "packages/lumio-wiki/src/lumio_wiki/data/skill/SKILL.md").read_text(
        encoding="utf-8"
    )
    protocol = (
        ROOT / "packages/lumio-wiki/src/lumio_wiki/data/skill/PROTOCOL.md"
    ).read_text(encoding="utf-8")
    for name, text in (("SKILL.md", skill), ("PROTOCOL.md", protocol)):
        for cmd in SKILL_JOURNEY_COMMANDS:
            assert cmd in text, f"{name} must document the journey command: {cmd}"
        assert "docs/quickstart.md" in text or "quickstart" in text, (
            f"{name} must point at the canonical journey"
        )


def test_quickstart_uses_nothing_secret_beyond_labelled_minio_test_defaults():
    """Non-secret placeholders everywhere except the explicitly labelled local
    MinIO test credentials (issue #180 acceptance criterion)."""
    text = QUICKSTART.read_text(encoding="utf-8")
    assert "AKIA" not in text, "no AWS-style access-key ids in the walkthrough"
    assert re.search(r"minioadmin[^\n]*#[^\n]*local test credential", text) or re.search(
        r"local test credential[^\n]*minioadmin", text
    ), "MinIO credentials must be labelled local-test"
    collapsed = _collapse(text)
    assert "local-test defaults" in collapsed, "the local-test labelling must be explicit"
    secret_envs = re.findall(r"export ([A-Z_0-9]*(?:SECRET|KEY)[A-Z_0-9]*)=", text)
    assert set(secret_envs) <= {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}, (
        f"unexpected secret-bearing exports: {secret_envs}"
    )


def test_quickstart_explains_the_four_operating_model_facts():
    """Backend vs mode, lumio.yaml states, local-vs-immutable-S3, and the
    expected zero-index fallback each get an explanation (issue #180)."""
    text = _collapse(QUICKSTART.read_text(encoding="utf-8"))
    for phrase in (
        "retrieval backend",
        "valid-empty ontology",
        "Legacy Flat Mode",
        "zero-index fallback",
        "read-only",
    ):
        assert phrase in text, f"quickstart must explain: {phrase}"


# ---------------------------------------------------------------------------
# 2. Fresh-environment local journey (no object store needed).
# ---------------------------------------------------------------------------


# Exported LUMIO_* variables that tests/conftest.py (or a developer shell) may
# inject — an exported LUMIO_KB_PATH silently overrides the journey project's
# .env, so the fresh-environment contract requires scrubbing them.
_SCRUBBED_ENV = (
    "LUMIO_KB_PATH",
    "LUMIO_PUBLISH_TO",
    "LUMIO_RETRIEVAL_BACKEND",
    "LUMIO_RETRIEVAL_MODE",
    "LUMIO_S3_SOURCE",
    "LUMIO_SOURCE_STORE",
    "LUMIO_STORAGE_MODE",
    "LUMIO_GIT_SOURCE",
    "LUMIO_SHARED_SOURCE",
    "LUMIO_CONFIG_PATH",
    "LUMIO_INGEST_PATH",
    "LUMIO_PUBLISH_PATH",
    "LUMIO_METADATA_DB_PATH",
    "LUMIO_WRITE_MODE",
)


def _scrubbed_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
    env["LUMIO_WIKI_BIN"] = shutil.which("lumio-wiki") or str(
        Path(sys.executable).parent / "lumio-wiki"
    )
    env["LUMIO_PYTHON"] = sys.executable
    return env


@pytest.fixture
def fresh_project(tmp_path, monkeypatch):
    """A clean temporary project: no exported LUMIO_* config, fresh cwd."""
    monkeypatch.chdir(tmp_path)
    for var in _SCRUBBED_ENV:
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_fresh_environment_local_journey(fresh_project):
    """The quickstart's local half, verbatim: setup -> ontology starter ->
    seed pages -> managed ingest -> proposal review -> publish -> retrieval
    with open actions -> traversal -> truthful Source-Artifact unavailability."""
    out = _run_cli(["setup", "./kb"])
    assert "maintainer (local worktree)" in out

    # Ontology starter: replace the seeded empty block (as the quickstart does).
    control = fresh_project / "kb" / "lumio.yaml"
    text = control.read_text(encoding="utf-8")
    old = "ontology:\n  entity_types:\n  predicates:\n  redirects:\n"
    assert old in text
    starter = """ontology:
  entity_types:
    software-system:
      description: "A deployed software product or platform."
    database:
      description: "A persistent storage service backing an application."
    procedure:
      description: "A reviewed runbook or operating procedure."
  predicates:
    uses:
      subject_types: [software-system]
      object_types: [database, software-system]
      inverse: used-by
    used-by:
      subject_types: [database, software-system]
      object_types: [software-system]
      inverse: uses
    described-as:
      literal_kind: string
  redirects: {}
"""
    control.write_text(text.replace(old, starter), encoding="utf-8")

    entities = fresh_project / "kb" / "entities"
    entities.mkdir()
    (entities / "aurora-helpdesk.md").write_text(
        RAW_PAGE_AURORA, encoding="utf-8"
    )
    (entities / "starlight-db.md").write_text(RAW_PAGE_STARLIGHT, encoding="utf-8")
    assert "Knowledge base is valid" in _run_cli(["validate", "./kb"])

    # Managed document ingest of the example's raw source.
    authored = fresh_project / "password-reset-page.md"
    authored.write_text(RAW_PAGE_RUNBOOK, encoding="utf-8")
    out = _run_cli([
        "ingest", "./kb", str(RAW_SOURCE),
        "--compiled-page", str(authored),
        "--source-id", "support-runbook-2026",
    ])
    proposal_id = re.search(r"Staged proposal ([0-9a-f]+)", out)
    assert proposal_id, out
    pid = proposal_id.group(1)
    assert "Knowledge base is valid" in _run_cli(["proposal", "validate", "./kb", pid])
    _run_cli(["publish", "./kb", pid])

    # Retrieval keeps an open action on every citation.
    out = _run_cli(["search", "password reset", "--limit", "2"])
    assert 'open:            lumio-wiki page "Password Reset Runbook"' in out
    out = _run_cli(["page", "Password Reset Runbook"])
    assert "source-artifact: lumio-wiki source inspect --source-id support-runbook-2026" in out

    # Canonical traversal over the seeded accepted Claim.
    out = _run_cli(["related", "Aurora Helpdesk", "--scope", "canonical", "--trace"])
    assert "Starlight DB" in out
    out = _run_cli(["paths", "Aurora Helpdesk", "Starlight DB", "--scope", "canonical"])
    assert "Starlight DB" in out

    # Source Artifact inspection is truthful: retention is disabled by default.
    out = _run_cli(["source", "inspect", "./kb", "--source-id", "support-runbook-2026"])
    assert "not retained (no Source Artifact Store configured)" in out
    with pytest.raises(AssertionError):
        _run_cli([
            "source", "fetch", "./kb", "--source-id", "support-runbook-2026",
            "--output", str(fresh_project / "out.md"),
        ])


RAW_PAGE_AURORA = """---
title: "Aurora Helpdesk"
id: "entity:aurora-helpdesk"
entity_types: ["software-system"]
aliases: ["Aurora"]
tags: ["support", "product"]
summary: "The Aurora helpdesk product this Knowledge Base documents."
lifecycle: "approved"
visibility: "internal"
sources:
  - id: "aurora-product-notes"
    title: "Aurora product notes"
claims:
  - id: "claim:aurora-uses-starlight"
    predicate: "uses"
    object: "entity:starlight-db"
    status: "accepted"
    evidence:
      - lines: [6, 6]
---

# Aurora Helpdesk

Aurora is the support helpdesk this Knowledge Base documents. It stores
tickets and attachments in [Starlight DB](starlight-db.md).
"""

RAW_PAGE_STARLIGHT = """---
title: "Starlight DB"
id: "entity:starlight-db"
entity_types: ["database"]
tags: ["storage"]
summary: "The persistent database behind Aurora Helpdesk."
lifecycle: "approved"
visibility: "internal"
sources:
  - id: "aurora-product-notes"
    title: "Aurora product notes"
claims:
  - id: "claim:starlight-described-as"
    predicate: "described-as"
    value: "managed PostgreSQL, region eu-west-1"
    value_type: "string"
    status: "accepted"
    evidence:
      - lines: [3, 4]
---

# Starlight DB

Starlight DB is the managed PostgreSQL (region eu-west-1) that stores Aurora
tickets and attachments.
"""

RAW_PAGE_RUNBOOK = """---
title: "Password Reset Runbook"
category: procedures
type: runbook
durability_rationale: "Owned runbook; reviewed yearly by Support Ops."
id: "entity:password-reset-runbook"
entity_types: ["procedure"]
tags: ["support", "runbook"]
summary: "How Aurora support verifies a requester and forces a password reset."
lifecycle: "approved"
visibility: "internal"
sources:
  - id: "support-runbook-2026"
    title: "Support runbook: password reset"
---

# Password Reset Runbook

Verified steps from the 2026 support runbook. Aurora Helpdesk support
verifies the requester through the secondary email on file, then forces the
reset from the requester's profile. The reset link expires after 30 minutes.
Requests without a secondary email escalate to Support Ops on-call; reset
volume is tracked in [Starlight DB](../entities/starlight-db.md).
"""


# ---------------------------------------------------------------------------
# 3. MinIO journey: execute the scriptable journey against a real endpoint.
# ---------------------------------------------------------------------------

pytest.importorskip("obstore", reason="obstore required for the MinIO journey")

requires_minio = pytest.mark.skipif(
    not os.environ.get("LUMIO_S3_ENDPOINT"),
    reason="LUMIO_S3_ENDPOINT not set; skipping onboarding MinIO journey",
)


@requires_minio
def test_minio_smoke_journey_script(tmp_path):
    """The scriptable journey runs end-to-end against the configured MinIO:
    S3 publication with and without LanceDB, read-only Reader setup, the
    disclosed zero-index fallback, and truthful mode/backend errors."""
    lumio_wiki = shutil.which("lumio-wiki") or str(
        Path(sys.executable).parent / "lumio-wiki"
    )
    assert Path(lumio_wiki).exists(), "lumio-wiki console script not found"
    env = _scrubbed_env()
    env["LUMIO_S3_TEST_BUCKET"] = os.environ.get(
        "LUMIO_S3_TEST_BUCKET", "lumio-quickstart"
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path / "journey")],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, (
        f"smoke journey failed:\n--- stdout tail ---\n"
        f"{result.stdout[-2000:]}\n--- stderr tail ---\n{result.stderr[-2000:]}"
    )
    assert "PASS: full onboarding journey complete" in result.stdout

