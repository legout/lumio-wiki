"""Focused regression checks for plan 04 agent-facing seams."""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path

import lumio_wiki as lw
import pytest
from lumio_wiki.cli import main
from lumio_wiki.distiller import _build_distill_system_prompt
from lumio_wiki.records import EntityTypeDefinition, Ontology, PredicateDefinition

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"


def _legacy_kb(tmp_path: Path, name: str = "kb") -> Path:
    root = tmp_path / name
    shutil.copytree(FIXTURES / "valid", root)
    return root


def test_page_machine_read_preserves_canonical_identity_and_bounds(tmp_path, capsys):
    root = _legacy_kb(tmp_path)
    canonical = (root / "overview.md").read_text(encoding="utf-8")

    assert main(["page", str(root), "Lumio Overview", "--raw", "--max-lines", "2", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["title"] == "Lumio Overview"
    assert result["path"] == "overview.md"
    assert result["content"] == "".join(canonical.splitlines(keepends=True)[:2])
    assert result["raw"] is True
    assert result["truncated"] is True
    assert result["omitted_lines"] > 0
    assert result["kb_location"] == str(root.resolve())

    assert main(["page", str(root), "Lumio Overview", "--max-lines", "1"]) == 0
    assert capsys.readouterr().out.startswith("---\n")


def test_machine_mutation_errors_are_stable_and_private(tmp_path, capsys):
    root = _legacy_kb(tmp_path)
    assert main(["publish", str(root), "missing-proposal", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)

    assert result == {
        "ok": False,
        "error": {
            "code": "publish_failed",
            "message": "proposal could not be published",
        },
    }
    assert "missing-proposal" not in capsys.readouterr().err


def test_copyable_actions_replay_original_kb(tmp_path, monkeypatch, capsys):
    first = _legacy_kb(tmp_path, "first")
    second = _legacy_kb(tmp_path, "second")
    source_store = lw.IngestStore(first / ".lumio" / "ingest")
    source_store.source_registry.register_source(
        "lumio-overview", b"source bytes", filename="overview.md"
    )

    assert main(["page", str(first), "Lumio Overview"]) == 0
    page_output = capsys.readouterr().out
    open_line = next(line for line in page_output.splitlines() if line.startswith("open:"))
    source_line = next(
        line for line in page_output.splitlines() if line.startswith("source-artifact:")
    )
    monkeypatch.setenv("LUMIO_KB_PATH", str(second))

    open_args = shlex.split(open_line.split(":", 1)[1].strip())
    assert main(open_args[1:]) == 0
    assert "# Lumio Overview" in capsys.readouterr().out

    source_args = shlex.split(source_line.split(":", 1)[1].strip())
    assert main(source_args[1:]) == 0
    source_output = capsys.readouterr().out
    assert "source_id:" in source_output
    assert "lumio-overview" in source_output
    assert str(first.resolve()) in source_line
    assert str(second.resolve()) not in source_line


def test_distiller_guidance_uses_current_kb_ontology():
    ontology = Ontology(
        entity_types={"service": EntityTypeDefinition(description="A deployed service.")},
        predicates={
            "depends-on": PredicateDefinition(
                subject_types=["service"],
                object_types=["service"],
                description="A runtime dependency.",
            )
        },
    )
    prompt = _build_distill_system_prompt(
        ["concepts"], ontology=ontology, authoring_mode="categorized"
    )

    assert "service — A deployed service." in prompt
    assert "depends-on" in prompt
    assert "A runtime dependency." in prompt
    assert "entity:<slug>" in prompt
    assert "accepted/disputed/superseded" in prompt
    assert "credentials" not in prompt


def test_real_anydoc_csv_cli_stages_reviewable_proposal(tmp_path, capsys):
    pytest.importorskip("anydoc")
    root = tmp_path / "kb"
    assert main(["init", str(root)]) == 0
    capsys.readouterr()
    csv = tmp_path / "records.csv"
    csv.write_text("name,value\nalpha,1\n", encoding="utf-8")

    assert main(["ingest", str(root), str(csv)]) == 0
    output = capsys.readouterr().out
    assert "Staged proposal" in output
    assert "converted_by:   anydoc" in output
    assert "blocked:" in output


def test_packaged_source_commands_execute_in_order(tmp_path, capsys):
    root = _legacy_kb(tmp_path)
    capsys.readouterr()
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    overview = root / "overview.md"
    overview.write_text(
        overview.read_text(encoding="utf-8") + "\nSee [Example](example.md).\n",
        encoding="utf-8",
    )
    page = tmp_path / "page.md"
    page.write_text(
        "---\n"
        "title: Example\n"
        "aliases: []\n"
        "tags: [example]\n"
        "summary: Example\n"
        "lifecycle: draft\n"
        "visibility: internal\n"
        "sources:\n  - id: example\n    title: Example\n"
        "---\n\n# Example\n\nBody.\n",
        encoding="utf-8",
    )
    assert (
        main(
            [
                "ingest",
                str(root),
                str(source),
                "--compiled-page",
                str(page),
                "--source-id",
                "example",
            ]
        )
        == 0
    )
    staged = capsys.readouterr().out
    proposal_id = next(part for part in staged.split() if len(part) == 32)
    assert main(["proposal", "inspect", str(root), proposal_id]) == 0
    capsys.readouterr()
    assert main(["proposal", "validate", str(root), proposal_id, "--json"]) == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation["proposal_id"] == proposal_id
    assert main(["publish", str(root), proposal_id, "--json"]) == 0
    published = json.loads(capsys.readouterr().out)
    assert published["ok"] is True


def test_protocol_denies_source_content_operational_authority():
    protocol = (Path(lw.__file__).parent / "data" / "skill" / "PROTOCOL.md").read_text(
        encoding="utf-8"
    )
    lower = protocol.lower()
    assert "untrusted data" in lower
    assert "authorize" in lower
    assert "never execute active" in lower
    assert "graph candidates" in lower or "citation existence" in lower


def test_readme_sample_validates_in_initialized_v2_kb():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "entity:technology-stack" in readme
    assert 'entity_types: ["software-system"]' in readme
    assert "lumio-wiki validate ./knowledge-base" in readme
