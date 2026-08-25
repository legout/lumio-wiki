"""Pre-1.0 release policy certification (issue #181).

Guards the invariants the release pipeline depends on:

1. **Lockstep family** — all three inter-member distributions carry one
   version. A tag builds all three wheels from that version
   (``.github/workflows/release.yml`` asserts it), so drifted member
   versions would publish a broken family.
2. **Bounded inter-member ranges** — every member-to-member dependency
   (core and extras) stays inside the released ``0.MINOR`` family. A
   family-crossing partial upgrade must fail at install time inside the
   resolver, naming the bounded range, instead of failing at runtime.
3. **Release notes** — the published version's notes cover the four
   topics issue #181 requires: migration, optional extras, supported
   Python version, and known MVP limits.
4. **Credential posture** — the release workflow publishes via trusted
   publishing only (no stored tokens), gates PyPI promotion behind a
   protected environment, and never triggers from pull requests.
"""

from __future__ import annotations

import re
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
MEMBERS = {
    "lumio-wiki": ROOT / "packages" / "lumio-wiki" / "pyproject.toml",
    "lumio-lancedb": ROOT / "packages" / "lumio-lancedb" / "pyproject.toml",
    "lumio": ROOT / "packages" / "lumio" / "pyproject.toml",
}
RELEASE_NOTES = ROOT / "docs" / "release-notes" / "v0.1.1.md"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"

MEMBER_NAMES = tuple(MEMBERS)


def _project(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]


def _all_requirements(project: dict) -> list[str]:
    requirements = list(project.get("dependencies", []))
    for extras in project.get("optional-dependencies", {}).values():
        requirements.extend(extras)
    return requirements


def test_members_share_one_lockstep_version():
    versions = {name: _project(path)["version"] for name, path in MEMBERS.items()}
    assert len(set(versions.values())) == 1, (
        "the three distributions release as one lockstep family "
        f"(issue #181 pre-1.0 policy); found {versions}"
    )


def test_inter_member_bounds_track_the_released_family():
    """Every member-to-member range spans exactly the released 0.MINOR family.

    Lower bound pins the released version; upper bound is the next minor.
    Ranges may reference extras (``lumio-wiki[s3]>=…``). A partial upgrade
    across families (e.g. ``lumio==0.2.0`` with ``lumio-wiki==0.1.1``) then
    fails inside pip/uv resolution, which names both bounded ranges — the
    actionable failure the acceptance criteria require.
    """
    released = _project(MEMBERS["lumio-wiki"])["version"]
    major, minor, _patch = (int(part) for part in released.split("."))
    expected = rf"lumio-(?:wiki|lancedb)(?:\[[a-z0-9,]+\])?>={released},<{major}.{minor + 1}.0"

    checked = 0
    for name, path in MEMBERS.items():
        for requirement in _all_requirements(_project(path)):
            # Extract the distribution name (before any extras/version
            # constraint) so ``lumio`` does not substring-match itself inside
            # ``lumio-wiki``.
            target = re.split(r"[\[<>=!~; ]", requirement, maxsplit=1)[0]
            if target in MEMBER_NAMES and target != name:
                assert re.fullmatch(expected, requirement), (
                    f"{name}: inter-member range {requirement!r} does not match "
                    f"the lockstep family {expected}"
                )
                checked += 1
    assert checked >= 4, "expected the documented member-to-member edges"


def test_release_notes_cover_the_required_topics():
    notes = RELEASE_NOTES.read_text(encoding="utf-8")
    lowered = notes.lower()
    # Issue #181 scope: migration, optional extras, supported Python
    # version, and known MVP limits.
    assert re.search(r"^#+ .*migrat", lowered, re.MULTILINE), "migration section"
    assert re.search(r"^#+ .*extras", lowered, re.MULTILINE), "extras section"
    assert re.search(r"^#+ .*python", lowered, re.MULTILINE), "python version section"
    assert re.search(r"^#+ .*mvp|^#+ .*limits", lowered, re.MULTILINE), "MVP limits section"
    assert "0.1.1" in notes


def test_release_workflow_uses_tag_trusted_publishing_and_gated_promotion():
    import yaml

    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))

    # Tag-driven only: never a pull_request trigger, so forks and PRs can
    # never reach a publishing job.
    assert set(workflow[True]) == {"push"}, workflow[True]
    assert workflow[True]["push"]["tags"] == ["v*.*.*"]
    assert "pull_request" not in workflow

    jobs = workflow["jobs"]
    assert "build-wheels" in jobs
    assert jobs["publish-testpypi"]["environment"] == "testpypi"
    assert jobs["promote-pypi"]["environment"] == "pypi"
    # The PyPI promotion is the human gate: it must depend on every
    # TestPyPI verification job.
    promote_needs = set(jobs["promote-pypi"]["needs"])
    verify_jobs = {name for name in jobs if name.startswith("verify-testpypi")}
    assert verify_jobs, "release must verify the TestPyPI install"
    assert verify_jobs <= promote_needs, promote_needs

    # Trusted publishing only: every publishing job holds exactly the OIDC
    # permission and no stored-token inputs anywhere in the workflow.
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "api_token" not in text and "password" not in text
    for job in ("publish-testpypi", "promote-pypi"):
        permissions = jobs[job].get("permissions") or {}
        assert permissions.get("id-token") == "write", (job, permissions)
