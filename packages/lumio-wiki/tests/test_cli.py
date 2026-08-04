"""Issue #98: the ``lumio-wiki`` CLI dispatches every public Knowledge Base
operation through its command surface.

These tests drive the CLI in-process (``lumio_wiki.cli.main``) so they are
fast and deterministic. The authoritative isolated-built-wheel proof lives in
``test_wheel_isolation.py``; this file locks in the command wiring, exit
codes, and output contracts against the package's own test fixtures.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import lumio_wiki as lw
import pytest
from lumio_wiki import GRAPH_ARTIFACT_FILENAME
from lumio_wiki.cli import build_parser, default_index_dir, main

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"

# Authored Compiled Page Markdown the host coding agent would produce.
PAGE_MD = textwrap.dedent(
    """\
    ---
    title: "CLI Page"
    aliases: []
    tags:
      - "cli"
    summary: "Authored through the lumio-wiki CLI."
    lifecycle: "draft"
    visibility: "internal"
    sources:
      - id: "cli"
        title: "CLI test source"
    relationships: []
    synthetic: false
    ---

    # CLI Page

    Body authored by the host agent.
    """
)


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    """Copy the valid fixture into a writable Knowledge Base root."""
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "valid", root)
    return root


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    """Write a test Knowledge Source file."""
    path = tmp_path / "source.md"
    path.write_text(PAGE_MD, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Dispatch and help
# ---------------------------------------------------------------------------


def test_build_parser_produces_lumio_wiki_prog():
    parser = build_parser()
    assert parser.prog == "lumio-wiki"


def test_no_command_prints_help_and_returns_1(capsys: pytest.CaptureFixture[str]):
    rc = main([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "lumio-wiki" in captured.out
    assert "init" in captured.out
    assert "validate" in captured.out


def test_version_flag_prints_package_version(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert lw.__version__ in captured.out


def test_all_documented_commands_have_handlers():
    """Every command the issue requires is registered with a callable handler."""
    parser = build_parser()
    # Walk the subparsers to find every registered command.
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, type(parser._subparsers._group_actions[0]))
    )
    # Top-level commands.
    expected_top_level = {
        "init",
        "validate",
        "search",
        "page",
        "related",
        "paths",
        "hot",
        "index",
        "ingest",
        "proposal",
        "publish",
        "discard",
        "health",
        "doctor",
        "skill",
    }
    assert expected_top_level <= set(subparsers_action.choices.keys())


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_creates_control_file_and_derived_dirs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    target = tmp_path / "newkb"
    rc = main(["init", str(target)])
    assert rc == 0
    assert (target / "lumio.yaml").is_file()
    assert (target / ".lumio" / "ingest").is_dir()
    assert (target / ".lumio" / "index").is_dir()
    out = capsys.readouterr().out
    assert "Initialized" in out


def test_init_refuses_existing_knowledge_base(tmp_path: Path):
    target = tmp_path / "existing"
    main(["init", str(target)])
    rc = main(["init", str(target)])
    assert rc == 2  # CliError exit code


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_valid_knowledge_base_returns_0(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(["validate", str(kb_root)])
    assert rc == 0
    assert "valid" in capsys.readouterr().out.lower()


def test_validate_invalid_knowledge_base_returns_1(tmp_path: Path):
    root = tmp_path / "bad"
    shutil.copytree(FIXTURES / "invalid", root)
    rc = main(["validate", str(root)])
    assert rc == 1


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_returns_matching_pages(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(["search", str(kb_root), "LanceDB", "--limit", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Architecture" in out or "Technology" in out


def test_search_no_matches_returns_0(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(["search", str(kb_root), "zzzznomatchzzzz"])
    assert rc == 0
    assert "No pages matched" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------


def test_page_reads_by_canonical_title(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(["page", str(kb_root), "Architecture"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# Architecture" in out
    assert "modular monolith" in out


def test_page_falls_back_to_alias(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    # The valid fixture declares "System Architecture" as an alias.
    rc = main(["page", str(kb_root), "System Architecture"])
    assert rc == 0
    assert "Architecture" in capsys.readouterr().out


def test_page_unknown_title_returns_1(kb_root: Path):
    rc = main(["page", str(kb_root), "Nonexistent Page"])
    assert rc == 1


# ---------------------------------------------------------------------------
# related + paths (graph traversal)
# ---------------------------------------------------------------------------


def test_related_lists_outgoing_titles(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(["related", str(kb_root), "Lumio Overview"])
    assert rc == 0
    assert "Architecture" in capsys.readouterr().out


def test_paths_finds_shortest_path(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(["paths", str(kb_root), "Lumio Overview", "Technology Stack"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert "Lumio Overview" in out
    assert "Technology Stack" in out
    assert "->" in out


def test_paths_no_path_returns_1(kb_root: Path):
    # A non-existent source title has no outgoing edges, so no path is found.
    rc = main(["paths", str(kb_root), "Nonexistent Source", "Architecture"])
    assert rc == 1


# ---------------------------------------------------------------------------
# ingest → proposal → publish → discard journey
# ---------------------------------------------------------------------------


def test_ingest_stages_reviewable_proposal(
    kb_root: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
):
    rc = main(["ingest", str(kb_root), str(source_file)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Staged proposal" in out
    assert "converted_by:   markdown" in out
    assert "CLI Page" in out


def test_proposal_list_shows_staged_proposal(kb_root: Path, source_file: Path):
    main(["ingest", str(kb_root), str(source_file)])
    # proposal list should show the staged proposal
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    proposals = store.list()
    assert len(proposals) == 1
    assert proposals[0].affected_pages == ["CLI Page"]


def test_proposal_inspect_prints_metadata_and_diff(
    kb_root: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
):
    main(["ingest", str(kb_root), str(source_file)])
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    pid = store.list()[0].id
    rc = main(["proposal", "inspect", str(kb_root), pid])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"id:              {pid}" in out
    assert "Diff:" in out


def test_proposal_inspect_json_emits_valid_json(
    kb_root: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
):
    main(["ingest", str(kb_root), str(source_file)])
    capsys.readouterr()  # drain ingest output before capturing JSON.
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    pid = store.list()[0].id
    rc = main(["proposal", "inspect", str(kb_root), pid, "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["id"] == pid
    assert data["affected_pages"] == ["CLI Page"]


def test_proposal_validate_reports_valid(kb_root: Path, source_file: Path):
    main(["ingest", str(kb_root), str(source_file)])
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    pid = store.list()[0].id
    rc = main(["proposal", "validate", str(kb_root), pid])
    assert rc == 0


def test_publish_writes_page_and_marks_terminal(
    kb_root: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
):
    main(["ingest", str(kb_root), str(source_file)])
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    pid = store.list()[0].id
    rc = main(["publish", str(kb_root), pid])
    assert rc == 0
    # The page was actually written to the KB root.
    assert (kb_root / "cli_page.md").is_file()
    # The proposal is now terminal.
    store2 = lw.IngestStore(kb_root / ".lumio" / "ingest")
    assert store2.get(pid).status == "published"
    # KB remains valid after publish.
    assert lw.validate(kb_root).is_valid


def test_discard_marks_proposal_terminal(kb_root: Path, source_file: Path):
    main(["ingest", str(kb_root), str(source_file)])
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    pid = store.list()[0].id
    rc = main(["discard", str(kb_root), pid])
    assert rc == 0
    store2 = lw.IngestStore(kb_root / ".lumio" / "ingest")
    assert store2.get(pid).status == "discarded"


def test_publish_unknown_proposal_returns_1(kb_root: Path):
    rc = main(["publish", str(kb_root), "nonexistent-id"])
    assert rc == 1


def test_lumio_dot_dir_does_not_pollute_fingerprint(kb_root: Path, source_file: Path):
    """The .lumio/ ingest store must not appear in the KB fingerprint."""
    fp_before = lw.fingerprint_sources(kb_root).digest
    main(["ingest", str(kb_root), str(source_file)])
    fp_after = lw.fingerprint_sources(kb_root).digest
    assert fp_before == fp_after


# ---------------------------------------------------------------------------
# ingest --distiller (issue #101)
# ---------------------------------------------------------------------------


def test_ingest_distiller_passthrough_is_the_default(kb_root: Path, source_file: Path):
    """The default distiller is the model-free passthrough (unchanged journey)."""
    parser = build_parser()
    args = parser.parse_args(["ingest", str(kb_root), str(source_file)])
    assert args.distiller == "passthrough"


def test_ingest_distiller_llm_without_extra_fails_actionably(
    kb_root: Path, source_file: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
):
    """AC4: requesting the llm distiller without the extra names the exact install.

    The base install (no ``openai`` module) must fail with the exact install
    command before any provider configuration is validated, so a missing extra
    is never masked by a missing-config error (ADR-0010, PRD user story 18).
    """
    # Ensure the extra is reported as absent (the dev env may or may not have
    # openai installed; force the missing-extra path deterministically).
    monkeypatch.setattr("lumio_wiki.cli._detect_module", lambda name: False)
    monkeypatch.setenv("LUMIO_PROVIDER_MODEL", "fake-model")
    rc = main(["ingest", str(kb_root), str(source_file), "--distiller", "llm"])
    assert rc == 2  # CliError exit code
    err = capsys.readouterr().err
    assert "pip install 'lumio-wiki[llm]'" in err, (
        "the missing-extra error must name the exact install command"
    )


def test_ingest_distiller_llm_with_fake_provider_stages_proposal(
    kb_root: Path,
    source_file: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    """AC2/AC3: ``--distiller llm`` with a fake provider stages a proposal."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    provider_md = (
        "---\n"
        'title: "LLM CLI Page"\n'
        "aliases: []\n"
        "tags:\n"
        '  - "llm"\n'
        'summary: "Distilled via the llm extra."\n'
        'lifecycle: "draft"\n'
        'visibility: "internal"\n'
        "sources:\n"
        '  - id: "llm"\n'
        '    title: "LLM source"\n'
        "relationships: []\n"
        "---\n\n# LLM CLI Page\n\nBody.\n"
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=provider_md))]
    )

    # Bypass real OpenAI client construction by patching the Distiller factory.
    import lumio_wiki

    real_openai_distiller = lumio_wiki.OpenAIDistiller

    class _StubDistiller(real_openai_distiller):
        def __init__(self, **kwargs):
            filtered = {
                k: v for k, v in kwargs.items() if k not in {"model", "base_url", "api_key"}
            }
            super().__init__(model="fake", client=fake_client, **filtered)

    monkeypatch.setattr(lumio_wiki, "OpenAIDistiller", _StubDistiller)
    monkeypatch.setenv("LUMIO_PROVIDER_MODEL", "fake-model")
    rc = main(["ingest", str(kb_root), str(source_file), "--distiller", "llm"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Staged proposal" in out
    assert "LLM CLI Page" in out


# ---------------------------------------------------------------------------
# health + doctor
# ---------------------------------------------------------------------------


def test_health_reports_page_count_and_graph(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(["health", str(kb_root)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "pages:" in out
    assert "graph_edges:" in out
    assert "fingerprint:" in out


def test_doctor_reports_version_optionals_and_skill(capsys: pytest.CaptureFixture[str]):
    rc = main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"lumio-wiki {lw.__version__}" in out
    assert "extra[documents]:" in out
    assert "extra[llm]:" in out
    assert "skill_md:" in out
    assert "skill_exists:" in out


# ---------------------------------------------------------------------------
# skill subcommands
# ---------------------------------------------------------------------------


def test_skill_path_prints_existing_skill(capsys: pytest.CaptureFixture[str]):
    from lumio_wiki.skill import resolve_skill_path

    rc = main(["skill", "path"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert str(resolve_skill_path()) == out
    assert Path(out).is_file()


def test_skill_protocol_prints_existing_protocol(capsys: pytest.CaptureFixture[str]):
    from lumio_wiki.skill import resolve_protocol_path

    rc = main(["skill", "protocol"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert str(resolve_protocol_path()) == out
    assert Path(out).is_file()


def test_skill_install_copies_into_agent_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    dest = tmp_path / "skills" / "lumio-wiki"
    rc = main(["skill", "install", "--agent", "pi", "--dest", str(dest)])
    assert rc == 0
    assert (dest / "SKILL.md").is_file()
    assert (dest / "PROTOCOL.md").is_file()


def test_skill_install_refuses_overwrite_without_flag(tmp_path: Path):
    dest = tmp_path / "skills" / "lumio-wiki"
    main(["skill", "install", "--agent", "pi", "--dest", str(dest)])
    rc = main(["skill", "install", "--agent", "pi", "--dest", str(dest)])
    assert rc != 0


def test_skill_install_overwrite_replaces_existing(tmp_path: Path):
    dest = tmp_path / "skills" / "lumio-wiki"
    main(["skill", "install", "--agent", "pi", "--dest", str(dest)])
    rc = main(["skill", "install", "--agent", "pi", "--dest", str(dest), "--overwrite"])
    assert rc == 0


def test_unsupported_agent_is_rejected():
    with pytest.raises(SystemExit):
        main(["skill", "install", "--agent", "unsupported-agent"])


# ---------------------------------------------------------------------------
# Issue #110: the zero-index retrieval ladder — graph bounds + truthful trace,
# Hot Index / Navigation Index surfaces, and graceful graph recovery.
# ---------------------------------------------------------------------------


@pytest.fixture
def categorized_kb(tmp_path: Path) -> Path:
    """Copy the categorized fixture (has Hot Index pins) into a writable root."""
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "categorized_kb", root)
    return root


# --- related / paths: explicit bounds + truthful trace (AC1) ---


def test_related_trace_reports_scope_direction_and_bounds(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    """AC1: --trace emits a truthful diagnostic of scope/direction/bounds."""
    rc = main(
        [
            "related",
            str(kb_root),
            "Lumio Overview",
            "--scope",
            "discovery",
            "--direction",
            "both",
            "--depth",
            "2",
            "--max-results",
            "5",
            "--trace",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "# trace:" in out
    assert "scope=discovery" in out
    assert "direction=both" in out
    assert "max_depth=2" in out
    assert "max_results=5" in out
    # The trace reports ONLY what the traversal used; it does not imply a
    # persisted artifact was the traversal source (ADR-0011 truthfulness).
    assert "artifact=" not in out


def test_paths_max_depth_bounds_traversal(kb_root: Path):
    """AC1: --max-depth is an explicit bound. A 2-hop path is unreachable at depth 1."""
    # Canonical path Lumio Overview -> Architecture -> Technology Stack is 2 hops.
    rc_shallow = main(
        ["paths", str(kb_root), "Lumio Overview", "Technology Stack", "--max-depth", "1"]
    )
    assert rc_shallow == 1  # bounded out — no path within 1 hop
    rc_deep = main(
        ["paths", str(kb_root), "Lumio Overview", "Technology Stack", "--max-depth", "2"]
    )
    assert rc_deep == 0


def test_paths_trace_reports_found_and_hops(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(
        [
            "paths",
            str(kb_root),
            "Lumio Overview",
            "Technology Stack",
            "--scope",
            "canonical",
            "--max-depth",
            "3",
            "--trace",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "# trace:" in out
    assert "scope=canonical" in out
    assert "max_depth=3" in out
    assert "found=true" in out
    assert "hops=2" in out


def test_paths_trace_reports_not_found(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(
        [
            "paths",
            str(kb_root),
            "Lumio Overview",
            "Architecture",
            "--max-depth",
            "0",
            "--trace",
        ]
    )
    # depth 0 forbids any hop, so no path to a different title.
    assert rc == 1
    out = capsys.readouterr().out
    assert "found=false" in out


# --- Hot Index + Navigation Index ladder entry points (AC2) ---


def test_hot_prints_pinned_hot_index(categorized_kb: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(["hot", str(categorized_kb)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Hot Index" in out
    assert "Lumio Overview" in out
    assert "Acme Corp" in out


def test_hot_reports_when_no_pins(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    # The valid fixture has no Control File → no pins.
    rc = main(["hot", str(kb_root)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No Hot Index" in out


def test_index_prints_root_navigation_index(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(["index", str(kb_root)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Lumio Overview" in out
    assert "Architecture" in out
    assert "Technology Stack" in out


def test_index_prints_subdirectory_index(categorized_kb: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(["index", str(categorized_kb), "concepts"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Lumio Overview" in out


# --- Graceful graph recovery (AC4) ---


def test_corrupt_artifact_does_not_block_zero_index_operation(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    """AC4: a corrupt graph artifact never blocks search/page/related/paths."""
    index_dir = default_index_dir(kb_root)
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / GRAPH_ARTIFACT_FILENAME).write_bytes(b"\x00\x01\x02 not msgpack")
    capsys.readouterr()  # clear
    # Every zero-index operation still works.
    assert main(["search", str(kb_root), "LanceDB"]) == 0
    assert main(["page", str(kb_root), "Architecture"]) == 0
    assert main(["related", str(kb_root), "Lumio Overview"]) == 0
    assert main(["paths", str(kb_root), "Lumio Overview", "Technology Stack"]) == 0
    # health reports the graph as not fresh (corrupt → ignored).
    rc = main(["health", str(kb_root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "graph_fresh:" in out and "False" in out


def test_health_reports_recovery_hint_when_graph_not_fresh(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    rc = main(["health", str(kb_root)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "graph_recovery:" in out
    assert "--rebuild" in out


def test_health_rebuild_materializes_fresh_graph(kb_root: Path, capsys: pytest.CaptureFixture[str]):
    index_dir = default_index_dir(kb_root)
    assert not (index_dir / GRAPH_ARTIFACT_FILENAME).exists()
    rc = main(["health", str(kb_root), "--rebuild"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "graph_materialized:" in out and "True" in out
    assert "graph_fresh:" in out and "True" in out
    assert (index_dir / GRAPH_ARTIFACT_FILENAME).is_file()


def test_health_rebuild_overwrites_corrupt_artifact(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    index_dir = default_index_dir(kb_root)
    index_dir.mkdir(parents=True, exist_ok=True)
    artifact = index_dir / GRAPH_ARTIFACT_FILENAME
    artifact.write_bytes(b"corrupt garbage")
    rc = main(["health", str(kb_root), "--rebuild"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "graph_fresh:" in out and "True" in out
    # Subsequent plain health (no rebuild) stays fresh, no recovery hint.
    capsys.readouterr()
    assert main(["health", str(kb_root)]) == 0
    out2 = capsys.readouterr().out
    assert "graph_fresh:" in out2 and "True" in out2
    assert "graph_recovery:" not in out2


# ---------------------------------------------------------------------------
# LUMIO_KB_PATH default (issue #125 follow-up)
# ---------------------------------------------------------------------------


def test_kb_path_defaults_to_env_var(monkeypatch, capsys):
    """LUMIO_KB_PATH is used when no positional <kb> is given."""
    monkeypatch.setenv("LUMIO_KB_PATH", str(FIXTURES / "valid"))
    rc = main(["search", "stack"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Technology Stack" in out


def test_kb_path_missing_gives_actionable_error(monkeypatch, capsys):
    """Neither positional nor env var → actionable error, exit 2."""
    monkeypatch.delenv("LUMIO_KB_PATH", raising=False)
    rc = main(["search", "query"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "LUMIO_KB_PATH" in err


def test_explicit_path_overrides_env_var(monkeypatch, capsys):
    """Positional <kb> takes precedence over LUMIO_KB_PATH."""
    monkeypatch.setenv("LUMIO_KB_PATH", "/nonexistent/path")
    rc = main(["search", str(FIXTURES / "valid"), "stack"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Technology Stack" in out


# ---------------------------------------------------------------------------
# setup command
# ---------------------------------------------------------------------------


def test_setup_creates_new_kb_and_config(tmp_path, monkeypatch, capsys):
    """setup creates KB, .env, and AGENTS.md from scratch."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LUMIO_KB_PATH", raising=False)
    kb_path = tmp_path / "my-wiki"

    rc = main(["setup", str(kb_path)])
    assert rc == 0

    # KB created
    assert (kb_path / "lumio.yaml").exists()
    assert (kb_path / ".lumio" / "ingest").is_dir()

    # .env written with absolute path
    env = (tmp_path / ".env").read_text()
    assert "LUMIO_KB_PATH=" in env
    assert str(kb_path.resolve()) in env

    # AGENTS.md written with the retrieval ladder
    agents_md = (tmp_path / "AGENTS.md").read_text()
    assert "## Lumio Knowledge Base" in agents_md
    assert "lumio-wiki search" in agents_md
    assert "retrieval ladder" in agents_md.lower()
    # ... and the Maintainer workflows (ADR-0015)
    assert "Maintenance (you are the Maintainer)" in agents_md
    assert "lumio-wiki lint" in agents_md
    assert "lumio-wiki cross-link" in agents_md
    assert "lumio-wiki dream" in agents_md


def test_setup_uses_existing_kb(tmp_path, monkeypatch, capsys):
    """setup does not overwrite an existing KB."""
    monkeypatch.chdir(tmp_path)
    kb_path = tmp_path / "existing"
    shutil.copytree(FIXTURES / "valid", kb_path)
    original_content = (kb_path / "technology.md").read_text()

    rc = main(["setup", str(kb_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already exists" in out

    # Content unchanged
    assert (kb_path / "technology.md").read_text() == original_content


def test_setup_no_agents_md_flag(tmp_path, monkeypatch):
    """--no-agents-md skips AGENTS.md writing."""
    monkeypatch.chdir(tmp_path)
    rc = main(["setup", str(tmp_path / "kb"), "--no-agents-md"])
    assert rc == 0
    assert not (tmp_path / "AGENTS.md").exists()


def test_setup_updates_existing_agents_md(tmp_path, monkeypatch):
    """setup replaces its section when AGENTS.md already has one."""
    monkeypatch.chdir(tmp_path)
    existing = "# My Project\n\nSome content.\n"
    (tmp_path / "AGENTS.md").write_text(existing)

    rc = main(["setup", str(tmp_path / "kb")])
    assert rc == 0

    content = (tmp_path / "AGENTS.md").read_text()
    assert "# My Project" in content  # original preserved
    assert "## Lumio Knowledge Base" in content  # section added


def test_setup_repeated_run_never_duplicates_the_section(tmp_path, monkeypatch):
    """Regression: repeated setup refreshes must not duplicate the KB section.

    The writer's replace span runs from the marker to the next `##` heading
    AFTER the section's own `## Lumio Knowledge Base` heading; terminating at
    the section's own heading prepended a fresh copy on every refresh.
    """
    monkeypatch.chdir(tmp_path)
    kb = tmp_path / "kb"
    for _ in range(3):
        assert main(["setup", str(kb)]) == 0
    content = (tmp_path / "AGENTS.md").read_text()
    assert content.count("## Lumio Knowledge Base") == 1
    assert content.count("<!-- lumio-wiki-kb -->") == 1


def test_setup_with_skill_install(tmp_path, monkeypatch, capsys):
    """setup --agent installs the skill into the agent's directory."""
    monkeypatch.chdir(tmp_path)
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    rc = main(["setup", str(tmp_path / "kb"), "--agent", "codex", "--overwrite"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "skill install:" in out

    skill_dir = fake_home / ".codex" / "skills" / "lumio-wiki"
    assert (skill_dir / "SKILL.md").exists()
    assert (skill_dir / "PROTOCOL.md").exists()


def test_setup_env_var_enables_implicit_path(tmp_path, monkeypatch, capsys):
    """After setup, LUMIO_KB_PATH from .env enables commands with no path arg."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LUMIO_KB_PATH", raising=False)
    kb_path = tmp_path / "wiki"
    shutil.copytree(FIXTURES / "valid", kb_path)

    # Setup writes .env with LUMIO_KB_PATH
    rc = main(["setup", str(kb_path)])
    assert rc == 0

    # Manually load .env (simulating what a shell/agent harness does)
    env_content = (tmp_path / ".env").read_text()
    for line in env_content.splitlines():
        if line.startswith("LUMIO_KB_PATH="):
            monkeypatch.setenv("LUMIO_KB_PATH", line.split("=", 1)[1])
            break

    # Now search with no path → uses LUMIO_KB_PATH
    rc = main(["search", "stack"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Technology Stack" in out


# ---------------------------------------------------------------------------
# Project .env loading (issue #152, ADR-0017)
#
# A fresh lumio-wiki subprocess must load LUMIO_KB_PATH from the project
# .env that `setup` wrote. The in-process tests below assert precedence
# (positional > exported env > .env > actionable error); the subprocess
# tests are the authoritative fresh-process proof the in-process
# monkeypatch.setenv simulation in the test above could not provide.
# ---------------------------------------------------------------------------


def _clean_subprocess_env() -> dict[str, str]:
    """A subprocess env without LUMIO_KB_PATH so only the project .env is seen."""
    return {key: value for key, value in os.environ.items() if key != "LUMIO_KB_PATH"}


def _run_cli_in_subprocess(
    args: list[str], cwd: Path, *, env_overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the CLI in a fresh process with optional environment overrides."""
    env = _clean_subprocess_env()
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


def test_env_file_loaded_when_no_positional_and_no_env_var(tmp_path: Path, monkeypatch, capsys):
    """With no positional and no exported var, LUMIO_KB_PATH is read from .env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LUMIO_KB_PATH", raising=False)
    kb = tmp_path / "kb"
    shutil.copytree(FIXTURES / "valid", kb)
    (tmp_path / ".env").write_text(f"LUMIO_KB_PATH={kb}\n", encoding="utf-8")

    rc = main(["search", "Technology"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Technology Stack" in out


def test_positional_path_overrides_env_file_and_env_var(tmp_path: Path, monkeypatch, capsys):
    """Positional <kb> takes precedence over both the env var and .env."""
    monkeypatch.chdir(tmp_path)
    real_kb = tmp_path / "real-kb"
    shutil.copytree(FIXTURES / "valid", real_kb)
    # Both lower-precedence sources point at nonexistent paths.
    monkeypatch.setenv("LUMIO_KB_PATH", str(tmp_path / "env-kb"))
    (tmp_path / ".env").write_text(f"LUMIO_KB_PATH={tmp_path / 'file-kb'}\n", encoding="utf-8")

    rc = main(["search", str(real_kb), "Technology"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Technology Stack" in out


def test_exported_env_var_overrides_env_file(tmp_path: Path, monkeypatch, capsys):
    """An exported LUMIO_KB_PATH wins over a .env value."""
    monkeypatch.chdir(tmp_path)
    real_kb = tmp_path / "real-kb"
    shutil.copytree(FIXTURES / "valid", real_kb)
    # .env points at a nonexistent path; the exported var points at the real KB.
    (tmp_path / ".env").write_text(f"LUMIO_KB_PATH={tmp_path / 'file-kb'}\n", encoding="utf-8")
    monkeypatch.setenv("LUMIO_KB_PATH", str(real_kb))

    rc = main(["search", "Technology"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Technology Stack" in out


def test_subprocess_positional_path_overrides_env_and_env_file(tmp_path: Path):
    """Fresh process: positional path outranks exported env and project .env."""
    project = tmp_path / "project"
    project.mkdir()
    real_kb = project / "real-kb"
    shutil.copytree(FIXTURES / "valid", real_kb)
    (project / ".env").write_text(f"LUMIO_KB_PATH={project / 'file-kb'}\n", encoding="utf-8")
    result = _run_cli_in_subprocess(
        ["search", str(real_kb), "Technology"],
        cwd=project,
        env_overrides={"LUMIO_KB_PATH": str(project / "env-kb")},
    )
    assert result.returncode == 0, result.stderr
    assert "Technology Stack" in result.stdout


def test_subprocess_exported_env_overrides_env_file(tmp_path: Path):
    """Fresh process: exported LUMIO_KB_PATH outranks project .env."""
    project = tmp_path / "project"
    project.mkdir()
    real_kb = project / "real-kb"
    shutil.copytree(FIXTURES / "valid", real_kb)
    (project / ".env").write_text(f"LUMIO_KB_PATH={project / 'file-kb'}\n", encoding="utf-8")
    result = _run_cli_in_subprocess(
        ["search", "Technology"],
        cwd=project,
        env_overrides={"LUMIO_KB_PATH": str(real_kb)},
    )
    assert result.returncode == 0, result.stderr
    assert "Technology Stack" in result.stdout


def test_empty_exported_env_does_not_fall_back_to_env_file(tmp_path: Path, monkeypatch, capsys):
    """An explicitly empty process value remains authoritative over .env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LUMIO_KB_PATH", "")
    (tmp_path / ".env").write_text(f"LUMIO_KB_PATH={tmp_path / 'valid-kb'}\n", encoding="utf-8")

    rc = main(["search", "Technology"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no Knowledge Base path provided" in err
    assert ".env" in err


def test_missing_or_malformed_env_file_gives_actionable_error(tmp_path: Path, monkeypatch, capsys):
    """A malformed .env (no key=value) yields a single actionable error."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LUMIO_KB_PATH", raising=False)
    # Not a KEY=value line, so LUMIO_KB_PATH is absent.
    (tmp_path / ".env").write_text("this line is malformed\n", encoding="utf-8")

    rc = main(["search", "query"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "LUMIO_KB_PATH" in err
    assert ".env" in err


def test_subprocess_invalid_env_path_is_actionable(tmp_path: Path):
    """A path value with an embedded NUL must not produce a traceback."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_bytes(b"LUMIO_KB_PATH=\x00\n")

    result = _run_cli_in_subprocess(["validate"], cwd=project)
    assert result.returncode == 2
    assert "no Knowledge Base path provided" in result.stderr
    assert "Traceback" not in result.stderr


def test_setup_help_claims_env_loading(capsys):
    """AC7: setup help/usage matches the tested .env-loading behavior."""
    with pytest.raises(SystemExit):
        main(["setup", "--help"])
    out = capsys.readouterr().out
    assert ".env" in out
    assert "LUMIO_KB_PATH" in out
    # argparse wraps the description, so assert a phrase that fits one line.
    assert "LUMIO_KB_PATH from .env" in out


def test_kb_path_help_documents_env_and_env_file(capsys):
    """AC7: the <kb> argument help documents the .env fallback."""
    with pytest.raises(SystemExit):
        main(["validate", "--help"])
    out = capsys.readouterr().out
    assert "LUMIO_KB_PATH" in out
    assert ".env" in out


def test_subprocess_setup_then_validate_reads_env(tmp_path: Path):
    """AC1: a fresh `lumio-wiki validate` reads LUMIO_KB_PATH from project .env.

    This is the authoritative fresh-subprocess reproduction of the bug report:
    setup writes .env, then a SEPARATE process validates with no positional
    path and no exported environment variable.
    """
    project = tmp_path / "project"
    project.mkdir()
    kb = project / "kb"
    shutil.copytree(FIXTURES / "valid", kb)

    setup = _run_cli_in_subprocess(["setup", str(kb)], cwd=project)
    assert setup.returncode == 0, setup.stderr

    validate = _run_cli_in_subprocess(["validate"], cwd=project)
    assert validate.returncode == 0, validate.stderr


def test_subprocess_journey_reads_env_for_path_commands(tmp_path: Path):
    """AC2: each path-bearing command resolves LUMIO_KB_PATH from .env.

    After setup, validate, search, page, proposal list, lint, and health all
    run with no positional <kb> and no exported variable in a fresh process.
    """
    project = tmp_path / "project"
    project.mkdir()
    shutil.copytree(FIXTURES / "valid", project / "kb")

    setup = _run_cli_in_subprocess(["setup", str(project / "kb")], cwd=project)
    assert setup.returncode == 0, setup.stderr

    journeys: list[tuple[list[str], str | None]] = [
        (["validate"], None),
        (["search", "Technology"], "Technology Stack"),
        (["page", "Technology Stack"], "Technology Stack"),
        (["proposal", "list"], None),
        (["lint"], None),
        (["health"], None),
    ]
    for command, expected in journeys:
        result = _run_cli_in_subprocess(command, cwd=project)
        assert result.returncode == 0, f"{command}: {result.stderr or result.stdout}"
        if expected is not None:
            assert expected in result.stdout, f"{command}: {result.stdout}"


def test_subprocess_env_file_does_not_leak_arbitrary_keys(tmp_path: Path):
    """AC6: reading .env never injects arbitrary keys into the process env."""
    project = tmp_path / "project"
    project.mkdir()
    shutil.copytree(FIXTURES / "valid", project / "kb")
    arbitrary = "LUMIO_TEST_LEAK_KEY_152"
    # Run setup, then append an arbitrary key to .env.
    setup = _run_cli_in_subprocess(["setup", str(project / "kb")], cwd=project)
    assert setup.returncode == 0, setup.stderr
    env_file = project / ".env"
    env_file.write_text(
        env_file.read_text(encoding="utf-8") + f"{arbitrary}=secret\n",
        encoding="utf-8",
    )
    # A fresh process that resolves the KB must not expose the arbitrary key.
    # Run a raw Python probe (not the CLI) so only the loader runs.
    probe_script = (
        "import os, sys; "
        "from lumio_wiki.env_loader import discover_kb_path_from_project_env; "
        "discover_kb_path_from_project_env(); "
        f"sys.stdout.write('LEAKED' if os.environ.get({arbitrary!r}) else 'clean')"
    )
    probe = subprocess.run(
        [sys.executable, "-c", probe_script],
        cwd=str(project),
        env=_clean_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    assert "clean" in probe.stdout, probe.stdout


# ---------------------------------------------------------------------------
# source subcommands: private Knowledge Source lifecycle (issue #133)
# ---------------------------------------------------------------------------

# A Compiled Page backed by the explicit ``policy`` Knowledge Source identity.
# The private Source Registry holds the identity; this page's ``sources[].id``
# is the ONLY thing that produces a retirement impact for ``policy`` — a
# renamed file registered under ``policy`` cannot create support for any page
# that does not declare it.
SOURCE_POLICY_PAGE_MD = textwrap.dedent(
    """\
    ---
    title: "CLI Page"
    aliases: []
    tags:
      - "cli"
    summary: "A Compiled Page backed by the policy Knowledge Source."
    lifecycle: "approved"
    visibility: "internal"
    sources:
      - id: "policy"
        title: "Policy Knowledge Source"
    relationships: []
    synthetic: false
    ---

    # CLI Page

    Backed by the policy Knowledge Source for lifecycle tests.
    """
)


@pytest.fixture
def source_kb(tmp_path: Path) -> Path:
    """A Knowledge Base whose ``CLI Page`` declares the ``policy`` source id."""
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "valid", root)
    (root / "cli_page.md").write_text(SOURCE_POLICY_PAGE_MD, encoding="utf-8")
    return root


def _extract_proposal_id(output: str) -> str:
    """Pull the staged proposal id from a ``Staged proposal <id>`` line."""
    return output.split("Staged proposal")[1].split()[0]


def _register_policy(kb: Path, source_file: Path) -> int:
    """Register the ``policy`` Knowledge Source and return the CLI exit code."""
    return main(
        ["source", "register", str(kb), "--source-id", "policy", "--file", str(source_file)]
    )


def test_source_retire_requires_explicit_registered_source_id(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["source", "retire", str(kb_root), "--source-id", "unknown"])
    assert rc == 1
    assert "unknown Knowledge Source" in capsys.readouterr().err


def test_source_retire_stages_safe_page_impact_output(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # ``source_kb`` contains ``sources: [{id: policy, ...}]`` for CLI Page.
    assert _register_policy(source_kb, source_file) == 0
    assert main(["source", "retire", str(source_kb), "--source-id", "policy"]) == 0
    output = capsys.readouterr().out
    assert "Staged proposal" in output
    assert "source_id:      policy" in output
    assert "sole-source-lost: CLI Page" in output
    assert str(source_file) not in output


def test_source_register_requires_explicit_source_id_and_file(source_kb: Path) -> None:
    # ``--source-id`` and ``--file`` are both required by the parser.
    with pytest.raises(SystemExit):
        main(["source", "register", str(source_kb), "--source-id", "policy"])
    with pytest.raises(SystemExit):
        main(["source", "register", str(source_kb), "--file", "/tmp/unused.md"])


def test_source_register_then_list_shows_active_identity(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _register_policy(source_kb, source_file) == 0
    capsys.readouterr()  # drain register output
    assert main(["source", "list", str(source_kb)]) == 0
    out = capsys.readouterr().out
    assert "policy" in out
    assert "active" in out
    # The listing never discloses raw bytes or the local source path.
    assert str(source_file) not in out


def test_source_candidate_leaves_source_active_without_staging(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    rc = main(
        [
            "source",
            "candidate",
            str(source_kb),
            "--source-id",
            "policy",
            "--trigger",
            "object store unavailable",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "policy" in out
    assert "object store unavailable" in out
    assert "pending" in out
    # Candidate-only behavior: the source stays active and no proposal stages.
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get("policy").status == "active"
    assert store.list() == []


def test_source_dismiss_candidate_records_decision_without_staging(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    main(
        [
            "source",
            "candidate",
            str(source_kb),
            "--source-id",
            "policy",
            "--trigger",
            "object store unavailable",
        ]
    )
    candidate_out = capsys.readouterr().out
    candidate_id = candidate_out.split("candidate_id:")[1].split()[0]
    rc = main(
        [
            "source",
            "dismiss-candidate",
            str(source_kb),
            "--candidate-id",
            candidate_id,
            "--source-id",
            "policy",
        ]
    )
    assert rc == 0
    dismiss_out = capsys.readouterr().out
    assert "dismissed" in dismiss_out
    # Dismiss records a decision without staging any proposal.
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get_candidate(candidate_id).status == "dismissed"
    assert store.list() == []


def test_source_dismiss_candidate_requires_explicit_source_id(
    source_kb: Path, source_file: Path
) -> None:
    # ``--source-id`` is required by the parser because dismissal mutates state.
    _register_policy(source_kb, source_file)
    with pytest.raises(SystemExit):
        main(
            [
                "source",
                "dismiss-candidate",
                str(source_kb),
                "--candidate-id",
                "deadbeef",
            ]
        )


def test_source_dismiss_candidate_refuses_mismatched_source_id(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    main(
        [
            "source",
            "candidate",
            str(source_kb),
            "--source-id",
            "policy",
            "--trigger",
            "object store unavailable",
        ]
    )
    candidate_out = capsys.readouterr().out
    candidate_id = candidate_out.split("candidate_id:")[1].split()[0]

    rc = main(
        [
            "source",
            "dismiss-candidate",
            str(source_kb),
            "--candidate-id",
            candidate_id,
            "--source-id",
            "wrong",
        ]
    )
    assert rc != 0
    err = capsys.readouterr().err
    assert "does not belong" in err
    # A mismatch must be safe: the candidate stays pending and nothing stages.
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get_candidate(candidate_id).status == "pending"
    assert store.list() == []


# --- #133 final review: secret-bearing invalid source/candidate ids at the CLI ---
#
# A crafted CLI request with a secret-bearing source/candidate id must fail
# with a GENERIC error: the secret, path, and hash never reach stdout/stderr,
# and no state mutates (no proposal staged, candidate/source unchanged).

_SECRET_SOURCE_ID = "sk-leaked-api-key;token=abc123"
_SECRET_CANDIDATE_ID = "/var/lib/lumio/secret.key"


def test_source_retire_rejects_secret_bearing_source_id(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    rc = main(["source", "retire", str(source_kb), "--source-id", _SECRET_SOURCE_ID])
    assert rc == 1
    combined = capsys.readouterr()
    assert _SECRET_SOURCE_ID not in (combined.out + combined.err)
    assert "secret.key" not in combined.err
    # No mutation: no proposal staged.
    assert lw.IngestStore(source_kb / ".lumio" / "ingest").list() == []


def test_source_reactivate_rejects_secret_bearing_source_id(
    source_kb: Path, source_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    replacement = tmp_path / "replacement.md"
    replacement.write_text("# replacement\n", encoding="utf-8")
    rc = main(
        [
            "source",
            "reactivate",
            str(source_kb),
            "--source-id",
            _SECRET_SOURCE_ID,
            "--file",
            str(replacement),
        ]
    )
    assert rc == 1
    combined = capsys.readouterr()
    assert _SECRET_SOURCE_ID not in (combined.out + combined.err)
    assert lw.IngestStore(source_kb / ".lumio" / "ingest").list() == []


def test_source_dismiss_rejects_secret_bearing_candidate_id(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()
    candidate_id = _record_candidate_cli(source_kb, capsys)
    rc = main(
        [
            "source",
            "dismiss-candidate",
            str(source_kb),
            "--candidate-id",
            _SECRET_CANDIDATE_ID,
            "--source-id",
            "policy",
        ]
    )
    assert rc == 1
    combined = capsys.readouterr()
    assert _SECRET_CANDIDATE_ID not in (combined.out + combined.err)
    # No mutation: the real candidate stays pending.
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get_candidate(candidate_id).status == "pending"


def test_source_confirm_rejects_secret_bearing_candidate_id(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()
    candidate_id = _record_candidate_cli(source_kb, capsys)
    rc = main(
        [
            "source",
            "confirm-candidate",
            str(source_kb),
            "--candidate-id",
            _SECRET_CANDIDATE_ID,
            "--source-id",
            "policy",
        ]
    )
    assert rc == 1
    combined = capsys.readouterr()
    assert _SECRET_CANDIDATE_ID not in (combined.out + combined.err)
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get_candidate(candidate_id).status == "pending"
    assert store.list() == []


def test_source_dismiss_rejects_secret_bearing_source_id(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()
    candidate_id = _record_candidate_cli(source_kb, capsys)
    rc = main(
        [
            "source",
            "dismiss-candidate",
            str(source_kb),
            "--candidate-id",
            candidate_id,
            "--source-id",
            _SECRET_SOURCE_ID,
        ]
    )
    assert rc == 1
    combined = capsys.readouterr()
    assert _SECRET_SOURCE_ID not in (combined.out + combined.err)
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get_candidate(candidate_id).status == "pending"


def test_source_confirm_rejects_secret_bearing_source_id(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()
    candidate_id = _record_candidate_cli(source_kb, capsys)
    rc = main(
        [
            "source",
            "confirm-candidate",
            str(source_kb),
            "--candidate-id",
            candidate_id,
            "--source-id",
            _SECRET_SOURCE_ID,
        ]
    )
    assert rc == 1
    combined = capsys.readouterr()
    assert _SECRET_SOURCE_ID not in (combined.out + combined.err)
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get_candidate(candidate_id).status == "pending"
    assert store.list() == []


def _record_candidate_cli(source_kb: Path, capsys: pytest.CaptureFixture[str]) -> str:
    """Record a ``policy`` retirement candidate via the CLI; return its id."""
    main(
        [
            "source",
            "candidate",
            str(source_kb),
            "--source-id",
            "policy",
            "--trigger",
            "object store unavailable",
        ]
    )
    candidate_out = capsys.readouterr().out
    return candidate_out.split("candidate_id:")[1].split()[0]


def test_source_retire_impacts_only_pages_declaring_the_source_id(
    source_kb: Path, source_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A renamed copy of the fixture source, registered under ``policy``, cannot
    # create support: only a Compiled Page whose ``sources[].id: policy`` is
    # impacted. The renamed filename and its bytes (which declare a different
    # source id) never influence page-level support.
    renamed = tmp_path / "renamed-policy-source.md"
    shutil.copyfile(source_file, renamed)
    assert (
        main(
            [
                "source",
                "register",
                str(source_kb),
                "--source-id",
                "policy",
                "--file",
                str(renamed),
            ]
        )
        == 0
    )
    capsys.readouterr()  # drain register output
    assert main(["source", "retire", str(source_kb), "--source-id", "policy"]) == 0
    out = capsys.readouterr().out
    assert "sole-source-lost: CLI Page" in out
    # The other fixture pages declare different source ids — never impacted.
    assert "Lumio Overview" not in out
    assert "Architecture" not in out
    assert "Technology Stack" not in out
    # The renamed file path never leaks into the output.
    assert str(renamed) not in out


def test_source_reactivate_stages_new_version_under_existing_id(
    source_kb: Path, source_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    assert main(["source", "retire", str(source_kb), "--source-id", "policy"]) == 0
    retire_id = _extract_proposal_id(capsys.readouterr().out)
    assert main(["publish", str(source_kb), retire_id]) == 0
    capsys.readouterr()  # drain publish output
    assert (
        lw.IngestStore(source_kb / ".lumio" / "ingest").source_registry.get("policy").status
        == "retired"
    )

    replacement = tmp_path / "replacement.md"
    replacement.write_text("# replacement policy bytes\n", encoding="utf-8")
    rc = main(
        [
            "source",
            "reactivate",
            str(source_kb),
            "--source-id",
            "policy",
            "--file",
            str(replacement),
        ]
    )
    assert rc == 0
    reactivate_out = capsys.readouterr().out
    assert "Staged proposal" in reactivate_out
    assert "source_id:      policy" in reactivate_out
    # Safe output: the replacement path and bytes never appear.
    assert str(replacement) not in reactivate_out
    reactivate_id = _extract_proposal_id(reactivate_out)

    # The source stays retired until the reactivation proposal publishes.
    assert (
        lw.IngestStore(source_kb / ".lumio" / "ingest").source_registry.get("policy").status
        == "retired"
    )
    assert main(["publish", str(source_kb), reactivate_id]) == 0
    active = lw.IngestStore(source_kb / ".lumio" / "ingest").source_registry.get("policy")
    assert active.status == "active"
    assert len(active.versions) == 2
    assert active.versions[0].source_id == active.versions[1].source_id == "policy"
    assert active.versions[0].content_hash != active.versions[1].content_hash


def test_source_reactivation_impact_counts_reactivated_source_as_support(
    source_kb: Path, source_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # #133 final review: reactivation evaluates support AFTER including the
    # reactivated source, so the sole-source ``CLI Page`` is still-supported
    # (not sole-source-lost, which is the retirement outcome for the same page).
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    assert main(["source", "retire", str(source_kb), "--source-id", "policy"]) == 0
    retire_id = _extract_proposal_id(capsys.readouterr().out)
    assert main(["publish", str(source_kb), retire_id]) == 0
    capsys.readouterr()  # drain publish output

    replacement = tmp_path / "replacement.md"
    replacement.write_text("# replacement policy bytes\n", encoding="utf-8")
    rc = main(
        [
            "source",
            "reactivate",
            str(source_kb),
            "--source-id",
            "policy",
            "--file",
            str(replacement),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    # Action-aware: the reactivated source itself restores support, so the
    # sole-source CLI Page renders still-supported.
    assert "still-supported: CLI Page" in out
    assert "sole-source-lost" not in out


# ---------------------------------------------------------------------------
# source file reads: safe errors with no path or traceback leakage
# ---------------------------------------------------------------------------


def test_source_register_missing_file_reports_generic_error_without_path(
    source_kb: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A missing source file must produce a generic, path-free user-facing error.
    missing = tmp_path / "does-not-exist.md"
    rc = main(
        ["source", "register", str(source_kb), "--source-id", "policy", "--file", str(missing)]
    )
    assert rc == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    # The supplied path (and its filename) never reaches the user, and no
    # traceback is leaked.
    assert str(missing) not in combined
    assert "does-not-exist.md" not in combined
    assert "Traceback" not in combined


def test_source_register_unreadable_file_reports_generic_error_without_path(
    source_kb: Path,
    source_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Existence/read race: the file exists but reading it raises OSError. The
    # raw OSError text (which may carry a path or credentials) must never reach
    # the user, and no traceback may leak.
    real_read_bytes = Path.read_bytes

    def raising_read_bytes(self: Path) -> bytes:
        if self == source_file:
            raise OSError("disk read failure at /secret/credentials.key")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", raising_read_bytes)

    rc = _register_policy(source_kb, source_file)

    assert rc == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert str(source_file) not in combined
    assert "/secret/credentials.key" not in combined
    assert "disk read failure" not in combined
    assert "Traceback" not in combined


def test_source_register_missing_registered_source_reports_generic_error(
    source_kb: Path,
    source_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Impossible-state guard: if the just-registered source is absent from the
    # public listing (an invariant the static type cannot prove is non-None),
    # the CLI must fail with a generic, path/secret-free error (rc=1) and never
    # leak a traceback from an unguarded ``None`` dereference.
    from lumio_wiki.proposal_pipeline import ProposalPipeline

    monkeypatch.setattr(ProposalPipeline, "list_sources", lambda self: [])

    rc = _register_policy(source_kb, source_file)

    assert rc == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Traceback" not in combined
    # The local source path (and any byte content) is never disclosed.
    assert str(source_file) not in combined


def test_source_reactivate_missing_file_reports_generic_error_without_path(
    source_kb: Path, source_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # ``--file`` is read through the same safe helper for reactivate.
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    retire_rc = main(["source", "retire", str(source_kb), "--source-id", "policy"])
    retire_id = _extract_proposal_id(capsys.readouterr().out)
    assert retire_rc == 0
    assert main(["publish", str(source_kb), retire_id]) == 0
    capsys.readouterr()  # drain publish output

    missing = tmp_path / "absent-replacement.md"
    rc = main(
        [
            "source",
            "reactivate",
            str(source_kb),
            "--source-id",
            "policy",
            "--file",
            str(missing),
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert str(missing) not in combined
    assert "absent-replacement.md" not in combined
    assert "Traceback" not in combined


def test_source_candidate_rejects_trigger_outside_vocabulary_safely(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # #133: a secret-bearing or arbitrary --trigger is rejected safely. The CLI
    # never echoes the value into its output and never persists a candidate.
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    secret = "password=hunter2;token=abc123"
    rc = main(
        [
            "source",
            "candidate",
            str(source_kb),
            "--source-id",
            "policy",
            "--trigger",
            secret,
        ]
    )
    assert rc != 0
    captured = capsys.readouterr()
    # The secret-bearing trigger is never echoed into stdout or stderr.
    assert secret not in captured.out
    assert secret not in captured.err
    # No candidate was persisted.
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.list_candidates() == []


def test_source_candidate_help_lists_controlled_trigger_choices(
    source_kb: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The --trigger help documents the exact controlled vocabulary so a
    # Maintainer knows the accepted signals without trial and error.
    with pytest.raises(SystemExit):
        main(["source", "candidate", str(source_kb), "--help"])
    out = capsys.readouterr().out
    # argparse wraps long help across lines; normalize whitespace (a display
    # detail) before asserting each controlled signal is documented.
    import re

    flat = re.sub(r"\s+", " ", out)
    for trigger in lw.RETIREMENT_CANDIDATE_TRIGGERS:
        assert trigger in flat


# ---------------------------------------------------------------------------
# source confirm-candidate: staged retirement through review (#133 final fix)
# ---------------------------------------------------------------------------


def _record_candidate(kb: Path, trigger: str = "watched file missing") -> str:
    """Record a retirement candidate for ``policy`` and return its id."""
    main(
        [
            "source",
            "candidate",
            str(kb),
            "--source-id",
            "policy",
            "--trigger",
            trigger,
        ]
    )
    out = _capture_candidate_output(kb)
    return out.split("candidate_id:")[1].split()[0]


def _capture_candidate_output(kb: Path) -> str:
    """Read the pending candidate id from the private registry (test only)."""
    import json

    registry = kb / ".lumio" / "ingest" / "source-registry" / "sources.json"
    state = json.loads(registry.read_text())
    for candidate in state.get("candidates", []):
        if candidate["source_id"] == "policy":
            return f"candidate_id: {candidate['id']}"
    raise AssertionError("no candidate recorded for policy")


def test_source_confirm_candidate_stages_retirement_proposal(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    candidate_id = _record_candidate(source_kb)
    capsys.readouterr()  # drain candidate output

    rc = main(
        [
            "source",
            "confirm-candidate",
            str(source_kb),
            "--source-id",
            "policy",
            "--candidate-id",
            candidate_id,
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Staged proposal" in out
    assert "source_id:      policy" in out
    # The candidate is marked confirmed and the source stays active until publish.
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get_candidate(candidate_id).status == "confirmed"
    assert store.source_registry.get("policy").status == "active"


def test_source_confirm_candidate_requires_explicit_ids(source_kb: Path) -> None:
    # Both --source-id and --candidate-id are required by the parser.
    with pytest.raises(SystemExit):
        main(["source", "confirm-candidate", str(source_kb), "--source-id", "policy"])
    with pytest.raises(SystemExit):
        main(["source", "confirm-candidate", str(source_kb), "--candidate-id", "deadbeef"])


def test_source_confirm_candidate_refuses_mismatched_source_id(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    candidate_id = _record_candidate(source_kb)
    capsys.readouterr()  # drain candidate output

    rc = main(
        [
            "source",
            "confirm-candidate",
            str(source_kb),
            "--source-id",
            "wrong",
            "--candidate-id",
            candidate_id,
        ]
    )
    assert rc != 0
    err = capsys.readouterr().err
    assert "does not belong" in err
    # A mismatch is safe: the candidate stays pending and no proposal stages.
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get_candidate(candidate_id).status == "pending"
    assert store.source_registry.get("policy").status == "active"
    assert store.list() == []


def test_source_confirm_candidate_leaves_source_active_until_publish(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_policy(source_kb, source_file)
    capsys.readouterr()  # drain register output
    candidate_id = _record_candidate(source_kb)
    capsys.readouterr()  # drain candidate output
    assert (
        main(
            [
                "source",
                "confirm-candidate",
                str(source_kb),
                "--source-id",
                "policy",
                "--candidate-id",
                candidate_id,
            ]
        )
        == 0
    )
    confirm_out = capsys.readouterr().out
    proposal_id = _extract_proposal_id(confirm_out)

    # The source is still active before the staged proposal publishes.
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert store.source_registry.get("policy").status == "active"
    assert main(["publish", str(source_kb), proposal_id]) == 0
    # Re-read from disk (each CLI command builds its own store instance, so the
    # in-memory state above is stale after the publish wrote the file).
    published_store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert published_store.source_registry.get("policy").status == "retired"


# ---------------------------------------------------------------------------
# register boundary: no duplicate creation, safe label contract (#133 final fix)
# ---------------------------------------------------------------------------


def test_source_duplicate_register_is_refused_without_mutation(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A second registration of an existing active id is refused generically and
    # never appends a version (#133 final review).
    assert _register_policy(source_kb, source_file) == 0
    capsys.readouterr()  # drain first register
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    versions_before = len(store.source_registry.get("policy").versions)

    rc = main(
        ["source", "register", str(source_kb), "--source-id", "policy", "--file", str(source_file)]
    )
    assert rc != 0
    err = capsys.readouterr().err
    assert "replacement is not available" in err
    # No mutation: the version count is unchanged and no second version appears.
    source = store.source_registry.get("policy")
    assert len(source.versions) == versions_before
    assert source.status == "active"


def test_source_register_rejects_invalid_source_id_safely(
    source_kb: Path, source_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A credential-shaped source id is rejected at the boundary; the rejected
    # value is never echoed into the CLI output and nothing is persisted.
    secret_id = "sk-leaked-api-key-1234567890"
    rc = main(
        ["source", "register", str(source_kb), "--source-id", secret_id, "--file", str(source_file)]
    )
    assert rc != 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert secret_id not in combined
    store = lw.IngestStore(source_kb / ".lumio" / "ingest")
    assert all(s.source_id != secret_id for s in store.source_registry.list())
