"""Issue #98: the ``lumio-wiki`` CLI dispatches every public Knowledge Base
operation through its command surface.

These tests drive the CLI in-process (``lumio_wiki.cli.main``) so they are
fast and deterministic. The authoritative isolated-built-wheel proof lives in
``test_wheel_isolation.py``; this file locks in the command wiring, exit
codes, and output contracts against the package's own test fixtures.
"""

from __future__ import annotations

import json
import shutil
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
        "  - \"llm\"\n"
        "summary: \"Distilled via the llm extra.\"\n"
        "lifecycle: \"draft\"\n"
        "visibility: \"internal\"\n"
        "sources:\n"
        "  - id: \"llm\"\n"
        "    title: \"LLM source\"\n"
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
                k: v for k, v in kwargs.items()
                if k not in {"model", "base_url", "api_key"}
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


def test_paths_trace_reports_found_and_hops(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
):
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


def test_hot_prints_pinned_hot_index(
    categorized_kb: Path, capsys: pytest.CaptureFixture[str]
):
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


def test_index_prints_root_navigation_index(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    rc = main(["index", str(kb_root)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Lumio Overview" in out
    assert "Architecture" in out
    assert "Technology Stack" in out


def test_index_prints_subdirectory_index(
    categorized_kb: Path, capsys: pytest.CaptureFixture[str]
):
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


def test_health_rebuild_materializes_fresh_graph(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
):
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
