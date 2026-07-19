from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
MEMBER = ROOT / "packages" / "lumio-wiki"


def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_uv_workspace_declares_lumio_wiki_and_explicit_member_dependency():
    root = _toml(ROOT / "pyproject.toml")
    assert root["tool"]["uv"]["workspace"]["members"] == ["packages/lumio-wiki"]
    assert "lumio-wiki>=0.1.1,<0.2.0" in root["project"]["dependencies"]
    assert root["tool"]["uv"]["sources"]["lumio-wiki"] == {"workspace": True}
    assert root["tool"]["pytest"]["ini_options"]["testpaths"] == [
        "tests",
        "packages/lumio-wiki/tests",
    ]

    member = _toml(MEMBER / "pyproject.toml")
    assert member["project"]["name"] == "lumio-wiki"
    assert member["project"]["requires-python"] == ">=3.14"
    assert member["project"]["dependencies"] == ["msgspec[yaml]>=0.21.1"]
    assert member["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/lumio_wiki"
    ]


def test_workspace_has_one_root_lockfile_and_disjoint_import_roots():
    assert (ROOT / "uv.lock").is_file()
    assert not (MEMBER / "uv.lock").exists()
    assert (ROOT / "src" / "lumio").is_dir()
    assert (MEMBER / "src" / "lumio_wiki").is_dir()
    assert not (MEMBER / "src" / "lumio").exists()


def test_workspace_ci_runs_package_and_application_consumer_suites():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    marker = "- run: >-\n          uv run --locked pytest -q\n"
    assert marker in workflow
    command = ("uv run --locked pytest -q\n" + workflow.split(marker, 1)[1])
    command = command.split("\n\n", 1)[0].split()
    assert command == [
        "uv",
        "run",
        "--locked",
        "pytest",
        "-q",
        "packages/lumio-wiki/tests",
        "tests/test_core_compatibility.py",
        "tests/test_workspace_layout.py",
        "tests/test_wheel_verifier.py",
        "tests/test_core_sdk.py",
        "tests/test_core_sdk_index.py",
        "tests/test_core_without_lancedb.py",
        "tests/test_zero_index_retrieval.py",
        "tests/test_retrieval_adapter_contract.py",
        "tests/test_kb_control.py",
        "tests/test_relationship_types.py",
        "tests/test_graph_retrieval.py",
        "tests/test_health_report.py",
        "tests/test_registry.py",
        "tests/test_okf_export.py",
        "tests/test_okf_import.py",
        "tests/test_okf_round_trip.py",
        "tests/test_okf_reject_unsafe.py",
    ]
