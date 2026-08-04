"""Cross-client Agent Skill installation and drift management (issue #150)."""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import lumio_wiki.skill as skill
import pytest
from lumio_wiki.cli import main


def test_shared_scope_destinations(tmp_path: Path):
    assert skill.scope_skill_dir("user", home=tmp_path) == (
        tmp_path / ".agents" / "skills" / "lumio-wiki"
    )
    project = tmp_path / "project"
    assert skill.scope_skill_dir("project", project_dir=project) == (
        project / ".agents" / "skills" / "lumio-wiki"
    )


@pytest.mark.parametrize(
    ("agent", "relative"),
    [
        ("pi", ".pi/agent/skills/lumio-wiki"),
        ("hermes", ".hermes/skills/lumio-wiki"),
        ("codex", ".codex/skills/lumio-wiki"),
        ("claude-code", ".claude/skills/lumio-wiki"),
    ],
)
def test_vendor_compatibility_destinations(tmp_path: Path, agent: str, relative: str):
    assert skill.agent_skill_dir(agent, home=tmp_path) == tmp_path / relative


def test_install_writes_traceable_manifest_and_reports_current(tmp_path: Path):
    target = skill.install_skill(scope="user", home=tmp_path)

    manifest = json.loads((target / skill.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest == {
        "schema_version": 1,
        "distribution": "lumio-wiki",
        "distribution_version": skill.package_version(),
        "content_hash": skill.packaged_contract_hash(),
        "source_contract_hash": skill.packaged_contract_hash(),
        "target": "scope:user",
    }
    status = skill.skill_status(scope="user", home=tmp_path)
    assert status.state == "current"
    assert status.installed_hash == status.packaged_hash


def test_status_reports_missing_without_writing(tmp_path: Path):
    target = skill.scope_skill_dir("user", home=tmp_path)
    status = skill.skill_status(scope="user", home=tmp_path)
    assert status.state == "missing"
    assert not target.exists()


def test_status_reports_legacy_manifestless_copy_as_stale(tmp_path: Path):
    target = skill.scope_skill_dir("user", home=tmp_path)
    target.mkdir(parents=True)
    (target / skill.SKILL_FILENAME).write_bytes(skill.resolve_skill_path().read_bytes())
    (target / skill.PROTOCOL_FILENAME).write_bytes(skill.resolve_protocol_path().read_bytes())

    status = skill.skill_status(scope="user", home=tmp_path)
    assert status.state == "stale"
    assert "legacy manifest-less" in status.detail
    skill.update_skill(scope="user", home=tmp_path)
    assert skill.skill_status(scope="user", home=tmp_path).state == "current"


def test_status_rejects_symlinked_manifest(tmp_path: Path):
    target = skill.install_skill(scope="user", home=tmp_path)
    manifest = target / skill.MANIFEST_FILENAME
    saved = tmp_path / "external-manifest.json"
    manifest.replace(saved)
    manifest.symlink_to(saved)

    status = skill.skill_status(scope="user", home=tmp_path)
    assert status.state == "corrupt"
    assert "not a regular file" in status.detail


def test_status_reports_corrupt_for_changed_content(tmp_path: Path):
    target = skill.install_skill(scope="user", home=tmp_path)
    (target / skill.SKILL_FILENAME).write_text("changed", encoding="utf-8")

    status = skill.skill_status(scope="user", home=tmp_path)
    assert status.state == "corrupt"
    assert "does not match" in status.detail


def test_status_reports_stale_and_update_refreshes(tmp_path: Path, monkeypatch):
    target = skill.install_skill(scope="user", home=tmp_path)
    original_version = skill.package_version()
    monkeypatch.setattr(skill, "package_version", lambda: f"{original_version}.next")

    assert skill.skill_status(scope="user", home=tmp_path).state == "stale"
    assert skill.update_skill(scope="user", home=tmp_path) == target
    assert skill.skill_status(scope="user", home=tmp_path).state == "current"


def test_update_repairs_corrupt_bundle(tmp_path: Path):
    target = skill.install_skill(scope="project", project_dir=tmp_path)
    (target / skill.PROTOCOL_FILENAME).unlink()

    assert skill.skill_status(scope="project", project_dir=tmp_path).state == "corrupt"
    skill.update_skill(scope="project", project_dir=tmp_path)
    assert skill.skill_status(scope="project", project_dir=tmp_path).state == "current"


def test_update_missing_requires_explicit_install(tmp_path: Path):
    with pytest.raises(skill.SkillError, match="run 'skill install' first"):
        skill.update_skill(scope="user", home=tmp_path)


def test_failed_atomic_update_restores_previous_bundle(tmp_path: Path, monkeypatch):
    target = skill.install_skill(scope="user", home=tmp_path)
    changed = b"locally changed skill"
    (target / skill.SKILL_FILENAME).write_bytes(changed)
    real_replace = os.replace

    def fail_activation(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.name.startswith(".lumio-wiki.tmp-") and destination_path == target:
            raise OSError("simulated activation failure")
        return real_replace(source, destination)

    monkeypatch.setattr(skill, "_atomic_directory_exchange", lambda _left, _right: False)
    monkeypatch.setattr(skill.os, "replace", fail_activation)
    with pytest.raises(skill.SkillError, match="could not activate"):
        skill.update_skill(scope="user", home=tmp_path)

    assert (target / skill.SKILL_FILENAME).read_bytes() == changed
    assert not list(target.parent.glob(".lumio-wiki.tmp-*"))
    assert not list(target.parent.glob(".lumio-wiki.backup-*"))


def test_interrupted_fallback_is_read_only_in_status_and_recovered_by_update(tmp_path: Path):
    target = skill.install_skill(scope="user", home=tmp_path)
    staged = skill._write_staged_bundle(target.parent, "scope:user")
    backup = target.parent / ".lumio-wiki.backup-interrupted"
    journal = skill._write_update_journal(target, staged, backup)
    os.replace(target, backup)  # simulate termination after the first fallback rename

    status = skill.skill_status(scope="user", home=tmp_path)
    assert status.state == "corrupt"
    assert journal.exists()
    assert backup.exists()
    assert not target.exists()

    skill.update_skill(scope="user", home=tmp_path)
    assert skill.skill_status(scope="user", home=tmp_path).state == "current"
    assert not journal.exists()
    assert not backup.exists()
    assert not staged.exists()


def test_concurrent_fallback_updates_are_serialized(tmp_path: Path, monkeypatch):
    target = skill.install_skill(scope="user", home=tmp_path)
    (target / skill.SKILL_FILENAME).write_text("corrupt", encoding="utf-8")
    barrier = threading.Barrier(4)
    real_status = skill.skill_status
    real_write_journal = skill._write_update_journal

    def synchronized_status(*args, **kwargs):
        status = real_status(*args, **kwargs)
        barrier.wait(timeout=5)
        return status

    def slow_journal(target_path, staged, backup):
        journal = real_write_journal(target_path, staged, backup)
        time.sleep(0.03)
        return journal

    monkeypatch.setattr(skill, "skill_status", synchronized_status)
    monkeypatch.setattr(skill, "_atomic_directory_exchange", lambda _left, _right: False)
    monkeypatch.setattr(skill, "_write_update_journal", slow_journal)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda _index: skill.update_skill(scope="user", home=tmp_path),
                range(4),
            )
        )

    monkeypatch.setattr(skill, "skill_status", real_status)
    assert results == [target] * 4
    assert real_status(scope="user", home=tmp_path).state == "current"
    assert not list(target.parent.glob(".lumio-wiki.tmp-*"))
    assert not list(target.parent.glob(".lumio-wiki.backup-*"))
    assert not skill._journal_path(target).exists()


def test_target_selection_rejects_ambiguous_or_unsafe_combinations(tmp_path: Path):
    with pytest.raises(skill.SkillError, match="either an agent target or a shared scope"):
        skill.skill_status("pi", scope="user", home=tmp_path)
    with pytest.raises(skill.SkillError, match="custom destination requires an agent"):
        skill.skill_status(dest=tmp_path / "custom")
    with pytest.raises(skill.SkillError, match="cannot be combined"):
        skill.skill_status(scope="project", dest=tmp_path / "custom")


def test_cli_install_and_status_shared_user_scope(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))

    assert main(["skill", "install", "--scope", "user"]) == 0
    target = tmp_path / ".agents" / "skills" / "lumio-wiki"
    assert (target / skill.MANIFEST_FILENAME).is_file()
    assert "Restart the agent" in capsys.readouterr().out

    assert main(["skill", "status", "--scope", "user"]) == 0
    assert "state:             current" in capsys.readouterr().out


def test_cli_status_missing_is_read_only(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))

    assert main(["skill", "status"]) == 1
    assert "state:             missing" in capsys.readouterr().out
    assert not (tmp_path / ".agents").exists()


def test_cli_update_repairs_corrupt_user_copy(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["skill", "install", "--scope", "user"]) == 0
    capsys.readouterr()
    target = tmp_path / ".agents" / "skills" / "lumio-wiki"
    (target / skill.SKILL_FILENAME).write_text("corrupt", encoding="utf-8")

    assert main(["skill", "update", "--scope", "user"]) == 0
    assert "corrupt -> current" in capsys.readouterr().out
    assert skill.skill_status(scope="user", home=tmp_path).state == "current"


def test_setup_installs_only_when_scope_is_explicit(tmp_path: Path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    assert main(["setup", "kb"]) == 0
    assert not (project / ".agents").exists()
    capsys.readouterr()

    assert main(["setup", "kb", "--skill-scope", "project"]) == 0
    target = project / ".agents" / "skills" / "lumio-wiki"
    assert skill.skill_status(scope="project", project_dir=project).state == "current"
    assert target.is_dir()
    assert "restart the agent" in capsys.readouterr().out.lower()
