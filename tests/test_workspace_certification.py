"""Publishable workspace certification invariants for issue #104 / ADR-0010.

The post-contraction workspace ships three independently buildable,
independently installable distributions: ``lumio-wiki``, ``lumio-lancedb``,
and ``lumio``. These tests assert the certification invariants that must
hold for every release:

* every member builds a wheel on its own (AC1);
* every inter-member dependency declares an explicit compatible version
  range — the shared lockfile is a development convenience, not a public
  compatibility contract (AC4);
* no two wheels own the same concrete Python module path, so installation
  and uninstallation cannot corrupt a shared namespace (ADR-0010); and
* the workspace lockfile is the single coordination point — no member
  carries its own lockfile.

The wheel-build assertion is marked ``slow`` because it shells out to
``uv build``; the metadata invariants run in every suite.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import tomllib

ROOT = Path(__file__).parents[1]
WIKI_MEMBER = ROOT / "packages" / "lumio-wiki"
LANCEDB_MEMBER = ROOT / "packages" / "lumio-lancedb"
APP_MEMBER = ROOT / "packages" / "lumio"
ALL_MEMBERS = {
    "lumio-wiki": WIKI_MEMBER,
    "lumio-lancedb": LANCEDB_MEMBER,
    "lumio": APP_MEMBER,
}

# Compatible published version range — every inter-member dependency must
# declare a bounded range so consumers that install from PyPI (without the
# workspace lockfile) get a reproducible resolution (ADR-0010, AC4).
LUMIO_RANGE_RE = re.compile(r"^lumio-(?:wiki|lancedb)>=\d+\.\d+\.\d+,<\d+\.\d+\.\d+$")


def _pyproject(member: Path) -> dict:
    return tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))


def _project_deps(member: Path) -> list[str]:
    data = _pyproject(member)
    return list(data["project"].get("dependencies", []))


# ---------------------------------------------------------------------------
# Metadata invariants — run in every suite.
# ---------------------------------------------------------------------------


def test_each_member_declares_an_independently_buildable_project():
    """AC1: each workspace member has the project + build backend to build alone."""
    for name, member in ALL_MEMBERS.items():
        data = _pyproject(member)
        project = data["project"]
        assert project["name"] == name, f"{member} declares name={project['name']!r}"
        assert project["requires-python"] == ">=3.14", name
        build = data["build-system"]
        assert build["requires"] == ["hatchling"], name
        assert build["build-backend"] == "hatchling.build", name
        wheel = data["tool"]["hatch"]["build"]["targets"]["wheel"]
        # Each wheel ships exactly one disjoint Python root (ADR-0010).
        assert len(wheel["packages"]) == 1, f"{name} must own exactly one root"


def test_published_member_dependencies_declare_compatible_version_ranges():
    """AC4: inter-member deps use bounded ranges, never the lockfile, never unbounded."""
    expected_edges = {
        # lumio-wiki is the foundation: it has no inter-member dependency.
        "lumio-wiki": {"lumio-wiki": 0, "lumio-lancedb": 0},
        # lumio-lancedb depends only on lumio-wiki.
        "lumio-lancedb": {"lumio-wiki": 1, "lumio-lancedb": 0},
        # lumio consumes both upward (ADR-0010).
        "lumio": {"lumio-wiki": 1, "lumio-lancedb": 1},
    }

    for name, member in ALL_MEMBERS.items():
        deps = _project_deps(member)
        # Every lumio-* dependency must use a bounded range.
        for dep in deps:
            if dep.startswith("lumio-"):
                assert LUMIO_RANGE_RE.match(dep), (
                    f"{name} dependency {dep!r} is not a bounded range "
                    f"(expected e.g. 'lumio-wiki>=0.1.1,<0.2.0')"
                )
        # Count inter-member edges and confirm they match the dependency graph.
        counts = {
            "lumio-wiki": sum(1 for d in deps if d.startswith("lumio-wiki")),
            "lumio-lancedb": sum(1 for d in deps if d.startswith("lumio-lancedb")),
        }
        assert counts == expected_edges[name], f"{name} dependency edges: {counts}"

    # The workspace lockfile is NOT a public compatibility contract: the
    # LUMIO_RANGE_RE check above already enforces that every ``lumio-*`` dep
    # carries both a lower and upper bound, rejecting bare names, unbounded
    # ``>=``, and workspace markers.


def test_optional_capability_extras_are_self_describing():
    """AC3/AC4: optional extras name the install line a user would type."""
    wiki = _pyproject(WIKI_MEMBER)
    extras = wiki["project"].get("optional-dependencies", {})
    # The base wheel stays model-free; only the optional extras pull weight.
    for extra in ("documents", "llm", "all"):
        assert extra in extras, f"lumio-wiki[{extra}] extra must be declared"
    # Each extra must be non-empty and self-contained — it cannot reference
    # another lumio-* distribution (ADR-0010: lumio-wiki has no downward dep).
    for name, deps in extras.items():
        assert deps, f"lumio-wiki[{name}] is empty"
        assert not any(d.startswith("lumio-") for d in deps), (
            f"lumio-wiki[{name}] pulls another lumio-* distribution"
        )

    lancedb = _pyproject(LANCEDB_MEMBER)
    lance_extras = lancedb["project"].get("optional-dependencies", {})
    assert "embeddings" in lance_extras, "lumio-lancedb[embeddings] extra must exist"
    # Torch stays out of the base adapter (ADR-0010).
    assert not any("torch" in d for d in lancedb["project"]["dependencies"])
    assert any(
        "sentence-transformers" in d for d in lance_extras["embeddings"]
    ), "lumio-lancedb[embeddings] must declare sentence-transformers"


def test_workspace_lockfile_is_the_single_coordination_point():
    """ADR-0010: one shared lockfile, no per-member lockfiles."""
    assert (ROOT / "uv.lock").is_file()
    for member in ALL_MEMBERS.values():
        assert not (member / "uv.lock").exists(), f"{member} carries its own uv.lock"


def test_no_two_wheels_own_the_same_python_module_path():
    """ADR-0010 / AC1: each wheel owns a single, disjoint Python root.

    The check runs against the declared hatchling package roots, which are
    what the built wheel actually ships. The wheel-level overlap check
    below (slow) re-confirms this against the built artifacts.
    """
    roots_by_member = {
        name: _pyproject(member)["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        for name, member in ALL_MEMBERS.items()
    }
    # Each member declares exactly one root.
    for name, roots in roots_by_member.items():
        assert len(roots) == 1, f"{name} declares {len(roots)} roots"
    # The roots are disjoint at the top-level Python package name.
    top_level = {name: Path(roots[0]).name for name, roots in roots_by_member.items()}
    assert len(set(top_level.values())) == 3, top_level
    assert set(top_level.values()) == {"lumio", "lumio_wiki", "lumio_lancedb"}, top_level


# ---------------------------------------------------------------------------
# Wheel-build certification — slow; only runs when ``-m slow`` is selected
# or from the repo root where uv is available.
# ---------------------------------------------------------------------------


def _has_uv() -> bool:
    return shutil.which("uv") is not None


@pytest.fixture(scope="module")
def built_wheels(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Build each member wheel once and return {member_name: wheel_path}."""
    if not _has_uv():
        pytest.skip("uv not available")
    out = tmp_path_factory.mktemp("lumio-cert-wheels")
    wheels: dict[str, Path] = {}
    for name in ALL_MEMBERS:
        run = subprocess.run(
            ["uv", "build", "--package", name, "--wheel", "--out-dir", str(out / name)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if run.returncode != 0:
            pytest.fail(f"`uv build --package {name}` failed:\n{run.stderr}")
        built = sorted((out / name).glob("*.whl"))
        assert built, f"no wheel produced for {name}"
        assert len(built) == 1, f"unexpected extra wheels for {name}: {built}"
        wheels[name] = built[0]
    return wheels


@pytest.mark.slow
def test_each_member_builds_a_wheel_independently(built_wheels: dict[str, Path]):
    """AC1: every member produces a wheel from the workspace without its siblings."""
    assert set(built_wheels) == {"lumio-wiki", "lumio-lancedb", "lumio"}


@pytest.mark.slow
def test_built_wheels_ship_disjoint_python_roots(built_wheels: dict[str, Path]):
    """AC1 + ADR-0010: the built artifacts themselves carry disjoint roots."""
    expected_roots = {
        "lumio-wiki": {"lumio_wiki"},
        "lumio-lancedb": {"lumio_lancedb"},
        "lumio": {"lumio"},
    }
    for name, wheel in built_wheels.items():
        roots: set[str] = set()
        with zipfile.ZipFile(wheel) as archive:
            for entry in archive.namelist():
                # Skip dist-info and data directories (where e.g. entry
                # names look like ``lumio_wiki-0.1.1.dist-info/METADATA``).
                top = entry.split("/", 1)[0]
                if not top or top.endswith(".dist-info") or top.endswith(".data"):
                    continue
                # The concrete Python root is the top-level package dir.
                root = top.split(".", 1)[0]
                if root.startswith("lumio"):
                    roots.add(root)
        assert roots == expected_roots[name], (
            f"{name} wheel ships unexpected roots {roots - expected_roots[name]} "
            f"or misses {(expected_roots[name] - roots)}"
        )


@pytest.mark.slow
def test_built_wheels_declare_bounded_inter_member_ranges(built_wheels: dict[str, Path]):
    """AC4: each built wheel's METADATA advertises bounded lumio-* ranges."""
    expected = {
        "lumio-wiki": {},
        "lumio-lancedb": {"lumio-wiki": {">=0.1.1", "<0.2.0"}},
        "lumio": {
            "lumio-wiki": {">=0.1.1", "<0.2.0"},
            "lumio-lancedb": {">=0.1.1", "<0.2.0"},
        },
    }
    for name, wheel in built_wheels.items():
        with zipfile.ZipFile(wheel) as archive:
            metadata_name = next(n for n in archive.namelist() if n.endswith("METADATA"))
            metadata = archive.read(metadata_name).decode("utf-8")
        # Parse each Requires-Dist into {dep_name: set(specs)} so the check is
        # robust against hatchling's specifier-order normalization (e.g.
        # ``<0.2.0,>=0.1.1`` instead of ``>=0.1.1,<0.2.0``).
        actual: dict[str, set[str]] = {}
        for line in metadata.splitlines():
            if not line.startswith("Requires-Dist: lumio-"):
                continue
            body = line[len("Requires-Dist: ") :]
            req_name = re.split(r"[<>=!~\[ ]", body, maxsplit=1)[0]
            specs = re.findall(r"(?:==|!=|>=|<=|~=|>|<)\s*\d+(?:\.\d+)*", body)
            actual[req_name] = {s.replace(" ", "") for s in specs}
        assert actual == expected[name], f"{name} METADATA lumio-* requires: {actual}"
