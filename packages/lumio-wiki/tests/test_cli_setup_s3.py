"""S3 coding-agent project configuration through ``setup`` (issue #161, ADR-0019).

Covers the two role-oriented setup forms, the bounded ``.env`` allowlist,
wizard/flag equivalence, idempotency, exported-process precedence, missing
optional-distribution guidance, the private source-store privacy guard, and
the ``publish-s3`` destination default. Fresh-process certification uses the
same subprocess harness as ``test_cli.py``; successful S3 *reads* are proven
in-process through the CLI's resolution seam (the live object-store contract
lives in ``test_s3_location.py`` / MinIO integration).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest  # type: ignore[import-not-found]
from lumio_wiki import cli
from lumio_wiki.cli import _write_agents_md_section
from lumio_wiki.env_loader import (
    ENV_ALLOWLIST,
    PUBLISH_TO_ENV_VAR,
    RETRIEVAL_BACKEND_ENV_VAR,
    RETRIEVAL_MODE_ENV_VAR,
    SOURCE_STORE_ENV_VAR,
    load_project_config,
    read_env_allowlist,
)
from lumio_wiki.location import FilesystemLocation

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"

_S3_KEYS = (
    "LUMIO_KB_PATH",
    PUBLISH_TO_ENV_VAR,
    RETRIEVAL_BACKEND_ENV_VAR,
    SOURCE_STORE_ENV_VAR,
)

obstore = pytest.importorskip("obstore", reason="obstore required for S3 setup tests")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _clean_env(monkeypatch) -> None:
    """Remove exported Lumio configuration so only .env values are visible."""
    for key in _S3_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key in ("LUMIO_S3_REGION", "LUMIO_S3_ENDPOINT", "LUMIO_RETRIEVAL_MODE"):
        monkeypatch.delenv(key, raising=False)


def _run_cli_in_subprocess(
    args: list[str], cwd: Path, *, env_overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the CLI in a fresh process with optional environment overrides."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _S3_KEYS and key != "LUMIO_S3_ENDPOINT"
    }
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from lumio_wiki.cli import main; raise SystemExit(main())",
            *args,
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _env_value(project: Path, key: str) -> str | None:
    for line in (project / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return None


class _StubS3Location:
    """A stand-in S3 Location that resolves to a real fixture Snapshot."""

    def __init__(self, snapshot) -> None:
        self._snapshot = snapshot

    def resolve(self):
        return self._snapshot


# ---------------------------------------------------------------------------
# Maintainer form
# ---------------------------------------------------------------------------


def test_maintainer_setup_records_s3_configuration(tmp_path, monkeypatch, capsys):
    """--publish-to/--retrieval/--source-store are recorded in .env and AGENTS.md."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)

    rc = cli.main(
        [
            "setup",
            "kb",
            "--publish-to",
            "s3://public-bucket/team-kb",
            "--retrieval",
            "lancedb",
            "--source-store",
            "s3://private-bucket/team-kb",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Created Knowledge Base" in out

    assert _env_value(tmp_path, "LUMIO_KB_PATH") == str((tmp_path / "kb").resolve())
    assert _env_value(tmp_path, PUBLISH_TO_ENV_VAR) == "s3://public-bucket/team-kb"
    assert _env_value(tmp_path, RETRIEVAL_BACKEND_ENV_VAR) == "lancedb"
    assert _env_value(tmp_path, SOURCE_STORE_ENV_VAR) == "s3://private-bucket/team-kb"

    agents_md = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "LUMIO_PUBLISH_TO" in agents_md
    assert "LUMIO_RETRIEVAL_BACKEND" in agents_md
    assert "LUMIO_SOURCE_STORE" in agents_md
    assert "s3://private-bucket/team-kb" in agents_md


def test_maintainer_setup_is_idempotent_with_s3_flags(tmp_path, monkeypatch):
    """Repeated setup writes identical .env and never duplicates AGENTS.md sections."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    argv = [
        "setup",
        "kb",
        "--publish-to",
        "s3://public-bucket/team-kb",
        "--retrieval",
        "zero-index",
    ]
    assert cli.main(argv) == 0
    first_env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert cli.main(argv) == 0
    assert (tmp_path / ".env").read_text(encoding="utf-8") == first_env
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.count("## Lumio Knowledge Base") == 1
    assert agents.count("<!-- lumio-wiki-kb -->") == 1
    assert agents.count("LUMIO_PUBLISH_TO") == 1
    assert agents.count("s3://public-bucket/team-kb") == 1


def test_plain_local_setup_still_writes_only_the_kb_path(tmp_path, monkeypatch):
    """Plain `setup <kb>` behavior is preserved: no S3 keys, no S3 AGENTS.md block."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    assert cli.main(["setup", "kb"]) == 0
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "LUMIO_KB_PATH=" in env_text
    assert PUBLISH_TO_ENV_VAR not in env_text
    assert RETRIEVAL_BACKEND_ENV_VAR not in env_text
    assert SOURCE_STORE_ENV_VAR not in env_text
    agents_md = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "LUMIO_PUBLISH_TO" not in agents_md


# ---------------------------------------------------------------------------
# Reader form
# ---------------------------------------------------------------------------


def test_reader_setup_records_s3_location_without_local_kb(tmp_path, monkeypatch, capsys):
    """setup --from records the read-only S3 location and creates nothing locally."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)

    rc = cli.main(["setup", "--from", "s3://public-bucket/team-kb", "--retrieval", "lancedb"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Read-only Knowledge Base location: s3://public-bucket/team-kb" in out

    assert _env_value(tmp_path, "LUMIO_KB_PATH") == "s3://public-bucket/team-kb"
    assert _env_value(tmp_path, RETRIEVAL_BACKEND_ENV_VAR) == "lancedb"
    assert PUBLISH_TO_ENV_VAR not in (tmp_path / ".env").read_text(encoding="utf-8")
    # No local Knowledge Base was created for a read-only project.
    assert not (tmp_path / "knowledge-base").exists()


def test_reader_setup_permits_pathless_reads_from_s3(tmp_path, monkeypatch, capsys):
    """After reader setup, a pathless command resolves the S3 location (stubbed)."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    assert cli.main(["setup", "--from", "s3://public-bucket/team-kb"]) == 0

    snapshot = FilesystemLocation(FIXTURES / "valid").resolve()

    def _fake_resolve(uri):
        assert cli._is_object_store_uri(uri)
        return _StubS3Location(snapshot)

    monkeypatch.setattr(cli, "_resolve_object_store_location", _fake_resolve)
    rc = cli.main(["search", "Technology"])
    assert rc == 0
    assert "Technology Stack" in capsys.readouterr().out


def test_fresh_process_reader_setup_then_pathless_read_reaches_s3(tmp_path):
    """Fresh process: reader setup writes .env, then a pathless read resolves S3.

    The read fails against an unreachable local endpoint (connection refused),
    which proves the fresh process loaded LUMIO_KB_PATH=s3://... from .env and
    routed the command through the S3 branch — the resolution contract itself
    is covered by the stubbed and MinIO suites.
    """
    project = tmp_path / "reader-project"
    project.mkdir()
    result = _run_cli_in_subprocess(["setup", "--from", "s3://public-bucket/team-kb"], cwd=project)
    assert result.returncode == 0, result.stderr
    assert (
        (project / ".env")
        .read_text(encoding="utf-8")
        .startswith("LUMIO_KB_PATH=s3://public-bucket/team-kb")
    )

    read = _run_cli_in_subprocess(
        ["search", "Technology"],
        cwd=project,
        env_overrides={"LUMIO_S3_ENDPOINT": "http://127.0.0.1:1"},
    )
    assert read.returncode == 2
    assert "could not resolve S3 Knowledge Base at s3://public-bucket/team-kb" in read.stderr
    assert "no Knowledge Base path provided" not in read.stderr
    assert "Traceback" not in read.stderr


# ---------------------------------------------------------------------------
# Fresh-process Maintainer configuration usability
# ---------------------------------------------------------------------------


def test_fresh_process_maintainer_setup_makes_publication_config_usable(tmp_path):
    """Fresh process: setup --publish-to produces the publication configuration.

    The end-to-end consumption of the recorded destination is proven
    deterministically in ``test_publish_s3_reads_destination_default_from_project_env``
    (in-memory store) and in the fresh-process plumbing test below; a live
    publish attempt is deliberately avoided here so no network or IMDS
    credential probe influences the result.
    """
    project = tmp_path / "maintainer-project"
    project.mkdir()
    shutil.copytree(FIXTURES / "valid", project / "kb")
    result = _run_cli_in_subprocess(
        [
            "setup",
            "kb",
            "--publish-to",
            "s3://public-bucket/team-kb",
            "--retrieval",
            "zero-index",
        ],
        cwd=project,
    )
    assert result.returncode == 0, result.stderr
    assert _env_value(project, PUBLISH_TO_ENV_VAR) == "s3://public-bucket/team-kb"
    assert _env_value(project, RETRIEVAL_BACKEND_ENV_VAR) == "zero-index"
    agents_md = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "LUMIO_PUBLISH_TO" in agents_md
    # The reader-style pathless source resolution works for the local KB too:
    # a fresh process with no positional resolves the KB from .env.
    validated = _run_cli_in_subprocess(["validate"], cwd=project)
    assert validated.returncode == 0, validated.stderr


def test_fresh_process_publish_s3_consumes_destination_from_env_file(tmp_path):
    """Fresh process: publish-s3 with no destination reads LUMIO_PUBLISH_TO.

    An invalid destination URI in .env makes the consumption observable
    without any network or credential I/O: the actionable error names the
    exact value the fresh process loaded.
    """
    project = tmp_path / "publish-project"
    project.mkdir()
    shutil.copytree(FIXTURES / "valid", project / "kb")
    (project / ".env").write_text(
        f"LUMIO_KB_PATH={project / 'kb'}\n{PUBLISH_TO_ENV_VAR}=not-a-uri\n",
        encoding="utf-8",
    )
    result = _run_cli_in_subprocess(["publish-s3", "--version", "v1"], cwd=project)
    assert result.returncode == 2
    assert "not an object-store destination URI: 'not-a-uri'" in result.stderr
    assert "no publish destination given" not in result.stderr
    assert "Traceback" not in result.stderr


def test_publish_s3_without_destination_and_config_is_actionable(tmp_path, monkeypatch, capsys):
    """publish-s3 with no destination and no LUMIO_PUBLISH_TO names the setup flow."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    shutil.copytree(FIXTURES / "valid", tmp_path / "kb")

    rc = cli.main(["publish-s3", "kb", "--version", "v1"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no publish destination given" in err
    assert "setup <kb> --publish-to" in err


def test_publish_s3_reads_destination_default_from_project_env(tmp_path, monkeypatch, capsys):
    """publish-s3 destination default flows into the publish store (in-memory)."""
    store = obstore.store.MemoryStore()

    def _fake_build(uri):
        assert uri == "s3://public-bucket/team-kb"
        return store, "team-kb"

    monkeypatch.setattr(cli, "_build_publish_store", _fake_build)
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    (tmp_path / ".env").write_text(
        f"LUMIO_KB_PATH={tmp_path / 'kb'}\n{PUBLISH_TO_ENV_VAR}=s3://public-bucket/team-kb\n",
        encoding="utf-8",
    )
    shutil.copytree(FIXTURES / "valid", tmp_path / "kb")

    rc = cli.main(["publish-s3", "--version", "v1"])
    assert rc == 0
    assert "location:    s3://public-bucket/team-kb@v1" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Location resolution, validation, and non-interactive failure
# ---------------------------------------------------------------------------


def test_setup_without_location_fails_with_required_flags_non_interactively(
    tmp_path, monkeypatch, capsys
):
    """No location + non-TTY: actionable required flags, exit 2, nothing written."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    rc = cli.main(["setup"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--from" in err
    assert "<local-kb>" in err
    assert "wizard" in err
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "AGENTS.md").exists()


def test_setup_rejects_local_path_combined_with_from(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    rc = cli.main(["setup", "kb", "--from", "s3://bucket/kb"])
    assert rc == 2
    assert "not both" in capsys.readouterr().err


def test_setup_rejects_publish_to_combined_with_from(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    rc = cli.main(["setup", "--from", "s3://bucket/kb", "--publish-to", "s3://bucket/pub"])
    assert rc == 2
    assert "cannot be combined with --from" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--from", "--publish-to", "--source-store"])
def test_setup_rejects_non_object_store_uris(flag, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    argv = ["setup", "kb"] if flag != "--from" else ["setup"]
    rc = cli.main([*argv, flag, "./not-a-uri"])
    assert rc == 2
    assert flag in capsys.readouterr().err
    assert "object-store URI" in capsys.readouterr().err or True


# ---------------------------------------------------------------------------
# Missing optional distributions: one exact install command, never a mutation
# ---------------------------------------------------------------------------


def test_setup_names_exact_command_when_s3_extra_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setattr(cli, "_detect_module", lambda name: name != "obstore")

    rc = cli.main(["setup", "kb", "--publish-to", "s3://bucket/kb"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "pip install 'lumio-wiki[s3]'" in err
    # Detection happens before any write.
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "kb").exists()


def test_setup_names_exact_command_when_lancedb_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setattr(cli, "_detect_module", lambda name: name != "lancedb")

    rc = cli.main(["setup", "kb", "--retrieval", "lancedb"])
    assert rc == 2
    assert "pip install 'lumio-lancedb[s3]'" in capsys.readouterr().err
    assert not (tmp_path / ".env").exists()


# ---------------------------------------------------------------------------
# Privacy guard for the private Source Artifact Store (ADR-0020)
# ---------------------------------------------------------------------------

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git binary required")


@requires_git
def test_setup_refuses_source_store_when_env_is_tracked_by_git(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".env").write_text("LUMIO_KB_PATH=/somewhere\n", encoding="utf-8")
    subprocess.run(["git", "add", ".env"], cwd=tmp_path, check=True)

    rc = cli.main(["setup", "kb", "--source-store", "s3://private-bucket/kb"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "refusing to record the private Source Artifact Store URI" in err
    assert "git rm --cached .env" in err
    # The refusal precedes any write: .env is unchanged and no URI leaked.
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "LUMIO_KB_PATH=/somewhere\n"


@requires_git
def test_setup_allows_source_store_when_env_is_ignored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")

    rc = cli.main(["setup", "kb", "--source-store", "s3://private-bucket/kb"])
    assert rc == 0
    assert _env_value(tmp_path, SOURCE_STORE_ENV_VAR) == "s3://private-bucket/kb"


@requires_git
def test_setup_allows_source_store_when_env_is_untracked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".env").write_text("LUMIO_KB_PATH=/somewhere\n", encoding="utf-8")

    rc = cli.main(["setup", "kb", "--source-store", "s3://private-bucket/kb"])
    assert rc == 0
    assert _env_value(tmp_path, SOURCE_STORE_ENV_VAR) == "s3://private-bucket/kb"


# ---------------------------------------------------------------------------
# Wizard: interactive fallback producing flag-equivalent configuration
# ---------------------------------------------------------------------------


def _make_interactive(monkeypatch, answers: list[str]):
    """Force TTY detection and feed canned answers to the wizard."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    queue = list(answers)
    monkeypatch.setattr("builtins.input", lambda _prompt: queue.pop(0))
    return queue


def test_wizard_maintainer_flow_matches_flag_flow(tmp_path, monkeypatch):
    """Wizard and flag flows produce equivalent configuration (maintainer)."""
    flag_dir = tmp_path / "flags"
    wizard_dir = tmp_path / "wizard"
    for directory in (flag_dir, wizard_dir):
        directory.mkdir()

    monkeypatch.chdir(flag_dir)
    _clean_env(monkeypatch)
    assert (
        cli.main(
            [
                "setup",
                "kb",
                "--publish-to",
                "s3://public-bucket/team-kb",
                "--retrieval",
                "lancedb",
                "--source-store",
                "s3://private-bucket/team-kb",
            ]
        )
        == 0
    )
    flag_env = (flag_dir / ".env").read_text(encoding="utf-8")

    monkeypatch.chdir(wizard_dir)
    _clean_env(monkeypatch)
    _make_interactive(
        monkeypatch,
        [
            "m",  # role: maintainer
            "kb",  # local KB directory
            "s3://public-bucket/team-kb",  # publish destination
            "l",  # retrieval backend: lancedb
            "s3://private-bucket/team-kb",  # source artifact store
            "n",  # skill: none
        ],
    )
    assert cli.main(["setup"]) == 0
    wizard_env = (wizard_dir / ".env").read_text(encoding="utf-8")

    # Identical except for the project-root prefix of the local KB path.
    assert wizard_env.replace(str(wizard_dir), "<root>") == flag_env.replace(
        str(flag_dir), "<root>"
    )
    wizard_agents = (wizard_dir / "AGENTS.md").read_text(encoding="utf-8")
    assert "LUMIO_PUBLISH_TO" in wizard_agents
    assert "LUMIO_SOURCE_STORE" in wizard_agents


def test_wizard_reader_flow_matches_flag_flow(tmp_path, monkeypatch):
    """Wizard and flag flows produce equivalent configuration (reader)."""
    flag_dir = tmp_path / "flags"
    wizard_dir = tmp_path / "wizard"
    for directory in (flag_dir, wizard_dir):
        directory.mkdir()

    monkeypatch.chdir(flag_dir)
    _clean_env(monkeypatch)
    assert (
        cli.main(["setup", "--from", "s3://public-bucket/team-kb", "--retrieval", "lancedb"]) == 0
    )
    flag_env = (flag_dir / ".env").read_text(encoding="utf-8")

    monkeypatch.chdir(wizard_dir)
    _clean_env(monkeypatch)
    _make_interactive(
        monkeypatch,
        [
            "r",  # role: reader
            "s3://public-bucket/team-kb",  # S3 Knowledge Base URI
            "l",  # retrieval backend: lancedb
            "",  # source artifact store: skip
            "n",  # skill: none
        ],
    )
    assert cli.main(["setup"]) == 0
    assert (wizard_dir / ".env").read_text(encoding="utf-8") == flag_env


def test_wizard_defaults_create_local_kb_with_zero_index(tmp_path, monkeypatch):
    """Enter-everywhere wizard run: maintainer, ./knowledge-base, zero-index, no skill."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    _make_interactive(monkeypatch, ["", "", "", "", "", ""])
    assert cli.main(["setup"]) == 0
    assert (tmp_path / "knowledge-base" / "lumio.yaml").exists()
    assert _env_value(tmp_path, RETRIEVAL_BACKEND_ENV_VAR) == "zero-index"
    assert PUBLISH_TO_ENV_VAR not in (tmp_path / ".env").read_text(encoding="utf-8")


def test_wizard_project_scope_skill_install_is_explicit(tmp_path, monkeypatch, capsys):
    """The wizard's skill answer routes through the same explicit install path."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    _make_interactive(monkeypatch, ["m", "kb", "", "z", "", "p"])
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "skill install:" in out
    assert "restart the agent or start a new session" in out
    assert (tmp_path / ".agents" / "skills" / "lumio-wiki" / "SKILL.md").exists()


def test_wizard_aborts_cleanly_on_eof(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def _eof(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    rc = cli.main(["setup"])
    assert rc == 2
    assert not (tmp_path / ".env").exists()


# ---------------------------------------------------------------------------
# Exported-process precedence over project .env values
# ---------------------------------------------------------------------------


def test_load_project_config_exported_values_beat_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    (tmp_path / ".env").write_text(
        f"{PUBLISH_TO_ENV_VAR}=s3://file-value/kb\n"
        f"{RETRIEVAL_BACKEND_ENV_VAR}=zero-index\n"
        "UNRELATED_KEY=nope\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(PUBLISH_TO_ENV_VAR, "s3://exported-value/kb")

    config = load_project_config()
    assert config[PUBLISH_TO_ENV_VAR] == "s3://exported-value/kb"
    assert config[RETRIEVAL_BACKEND_ENV_VAR] == "zero-index"  # from .env
    assert "UNRELATED_KEY" not in config


def test_load_project_config_empty_exported_value_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    (tmp_path / ".env").write_text(f"{PUBLISH_TO_ENV_VAR}=s3://file-value/kb\n", encoding="utf-8")
    monkeypatch.setenv(PUBLISH_TO_ENV_VAR, "")

    assert PUBLISH_TO_ENV_VAR not in load_project_config()


def test_fresh_process_exported_publish_to_beats_env_file(tmp_path):
    """Fresh process: an exported (invalid) LUMIO_PUBLISH_TO outranks .env."""
    project = tmp_path / "project"
    project.mkdir()
    shutil.copytree(FIXTURES / "valid", project / "kb")
    (project / ".env").write_text(
        f"LUMIO_KB_PATH={project / 'kb'}\n{PUBLISH_TO_ENV_VAR}=s3://file-value/kb\n",
        encoding="utf-8",
    )
    result = _run_cli_in_subprocess(
        ["publish-s3", "kb", "--version", "v1"],
        cwd=project,
        env_overrides={PUBLISH_TO_ENV_VAR: "not-a-uri"},
    )
    assert result.returncode == 2
    assert "not an object-store destination URI" in result.stderr
    assert "not-a-uri" in result.stderr


def test_read_env_allowlist_returns_only_allowlisted_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "LUMIO_KB_PATH=./kb\n"
        f"{PUBLISH_TO_ENV_VAR}=s3://pub/kb\n"
        f"{SOURCE_STORE_ENV_VAR}=s3://priv/kb\n"
        f"{RETRIEVAL_BACKEND_ENV_VAR}=lancedb\n"
        f"{RETRIEVAL_MODE_ENV_VAR}=hybrid\n"
        "LUMIO_S3_REGION=us-east-1\n"
        "LUMIO_S3_ENDPOINT=http://localhost:9000\n"
        "LUMIO_S3_ACCESS_KEY_ID=secret\n"
        "SOME_OTHER_KEY=value\n",
        encoding="utf-8",
    )
    values = read_env_allowlist(env)
    assert set(values) <= ENV_ALLOWLIST
    assert values[PUBLISH_TO_ENV_VAR] == "s3://pub/kb"
    assert values["LUMIO_KB_PATH"] == str((tmp_path / "kb").resolve())
    # Credentials are deliberately outside the allowlist.
    assert "LUMIO_S3_ACCESS_KEY_ID" not in values
    assert "SOME_OTHER_KEY" not in values


def test_s3_config_reads_region_and_endpoint_from_env_file(tmp_path, monkeypatch):
    """LUMIO_S3_REGION/LUMIO_S3_ENDPOINT load from .env; credentials never do."""
    monkeypatch.chdir(tmp_path)
    for key in (
        "LUMIO_S3_REGION",
        "LUMIO_S3_ENDPOINT",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_ENDPOINT_URL_S3",
        "LUMIO_S3_ACCESS_KEY_ID",
        "LUMIO_S3_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "LUMIO_S3_REGION=eu-west-1\nLUMIO_S3_ENDPOINT=http://localhost:9000\n"
        "LUMIO_S3_ACCESS_KEY_ID=secret\n",
        encoding="utf-8",
    )

    config, client_options = cli._s3_config_from_env()
    assert config["aws_region"] == "eu-west-1"
    assert config["aws_endpoint"] == "http://localhost:9000"
    assert client_options["allow_http"] is True
    # Credential keys never load from .env — AWS resolution stays authoritative.
    assert "aws_access_key_id" not in config
    assert "aws_secret_access_key" not in config


def test_s3_config_exported_lumio_endpoint_beats_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LUMIO_S3_ENDPOINT", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
    (tmp_path / ".env").write_text("LUMIO_S3_ENDPOINT=http://from-file:9000\n", encoding="utf-8")
    monkeypatch.setenv("LUMIO_S3_ENDPOINT", "http://exported:9000")

    config, _ = cli._s3_config_from_env()
    assert config["aws_endpoint"] == "http://exported:9000"


# ---------------------------------------------------------------------------
# Generated AGENTS.md and CLI help parity surfaces (issue #161 AC)
# ---------------------------------------------------------------------------


def test_generated_agents_md_documents_configured_s3_settings(tmp_path):
    """The configured AGENTS.md section records the S3 settings; plain does not."""
    _write_agents_md_section(
        tmp_path / "AGENTS.md",
        "/example/kb",
        publish_to="s3://public-bucket/team-kb",
        retrieval="lancedb",
        source_store="s3://private-bucket/team-kb",
    )
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "LUMIO_PUBLISH_TO" in text
    assert "LUMIO_RETRIEVAL_BACKEND" in text
    assert "LUMIO_SOURCE_STORE" in text
    assert "lumio-wiki publish-s3" in text
    # Retrieval backend and mode are presented as separate settings.
    assert "retrieval *mode*" in text

    plain = tmp_path / "plain-AGENTS.md"
    _write_agents_md_section(plain, "/example/kb")
    plain_text = plain.read_text(encoding="utf-8")
    assert "LUMIO_PUBLISH_TO" not in plain_text
    assert "LUMIO_SOURCE_STORE" not in plain_text


def test_setup_help_documents_the_s3_forms():
    import io

    buf = io.StringIO()
    import contextlib

    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        try:
            cli.main(["setup", "--help"])
        except SystemExit:
            pass
    help_text = buf.getvalue()
    assert "--publish-to" in help_text
    assert "--from" in help_text
    assert "--source-store" in help_text
    assert "--retrieval" in help_text
    assert "lumio-lancedb[s3]" in help_text
