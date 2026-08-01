from __future__ import annotations

import importlib.metadata
import importlib.util
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

FORBIDDEN = {
    "lancedb",
    "pyarrow",
    "stario",
    "piccolo",
    "openai",
    "liteparse",
    "markitdown",
}


def _python_roots(wheel: Path) -> set[str]:
    roots: set[str] = set()
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            parts = name.split("/")
            if parts[0].endswith(".data"):
                if len(parts) < 3 or parts[1] not in {"purelib", "platlib"}:
                    continue
                name = "/".join(parts[2:])

            root, separator, _ = name.partition("/")
            if root.endswith(".dist-info"):
                continue
            if not name.endswith((".py", ".so", ".pyd")):
                continue
            roots.add(root if separator else root.split(".", 1)[0])
    return roots


def _wheel_requirements(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_name).decode("utf-8")
    return [
        line.removeprefix("Requires-Dist: ").strip()
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist: ")
    ]


def main() -> int:
    if len(sys.argv) != 5:
        raise SystemExit(
            "usage: verify_lumio_wiki_wheel.py WIKI_WHEEL APP_WHEEL "
            "VALID_FIXTURE CATEGORIZED_FIXTURE"
        )
    wiki_wheel, app_wheel, valid_fixture, categorized_fixture = map(Path, sys.argv[1:])

    assert _python_roots(wiki_wheel) == {"lumio_wiki"}
    assert _python_roots(app_wheel) == {"lumio"}
    assert _python_roots(wiki_wheel).isdisjoint(_python_roots(app_wheel))

    requirements = importlib.metadata.requires("lumio-wiki") or []
    # Core (unconditional) runtime deps only — exclude optional extras gated
    # by environment markers (the "; extra == ..." entries). lumio-wiki must
    # stay lightweight and model-free (ADR-0010): pinning the exact core set
    # guards against an accidental heavy dependency landing in the base wheel.
    # ``msgpack`` is the Discovery Graph serialization format (#108);
    # ``msgspec[yaml]`` is the compiled-Markdown struct codec.
    core_requirements = sorted(
        requirement.replace(" ", "").lower()
        for requirement in requirements
        if ";" not in requirement
    )
    assert core_requirements == ["msgpack>=1.0", "msgspec[yaml]>=0.21.1"], core_requirements

    app_requirements = [item.replace(" ", "").lower() for item in _wheel_requirements(app_wheel)]
    # The app's CORE dependency on lumio-wiki; ignore the optional
    # ``lumio-wiki[s3]`` entry that appears under the app's own ``s3`` extra.
    app_member = [
        item for item in app_requirements if item.startswith("lumio-wiki") and ";" not in item
    ]
    assert len(app_member) == 1, app_requirements
    assert ">=0.1.1" in app_member[0] and "<0.2.0" in app_member[0]
    assert all(importlib.util.find_spec(name) is None for name in FORBIDDEN)
    installed = {
        distribution.metadata["Name"].lower().replace("_", "-")
        for distribution in importlib.metadata.distributions()
    }
    assert {"lumio-wiki", "msgspec"} <= installed
    assert installed.isdisjoint(FORBIDDEN), sorted(installed & FORBIDDEN)

    import lumio_wiki

    kb, report = lumio_wiki.load_knowledge_base(valid_fixture)
    assert report.is_valid
    assert lumio_wiki.validate(valid_fixture).is_valid
    assert kb.fingerprint().digest == lumio_wiki.fingerprint_sources(valid_fixture).digest
    assert kb.search_pages("LanceDB")
    assert kb.lookup_by_title("Architecture")[0].body
    assert kb.related_from("Lumio Overview")
    assert kb.graph_path("Lumio Overview", "Technology Stack")
    assert kb.retrieve("Lumio uses LanceDB", limit=2)
    health = kb.health_report()
    assert not health.broken_relationships

    with tempfile.TemporaryDirectory() as temporary:
        regenerated = Path(temporary) / "kb"
        shutil.copytree(categorized_fixture, regenerated)
        lumio_wiki.regenerate_reserved_artifacts(regenerated)
        assert (regenerated / "index.md").is_file()
        assert (regenerated / "hot.md").is_file()
        categorized, categorized_report = lumio_wiki.load_knowledge_base(regenerated)
        assert categorized_report.is_valid
        assert categorized.control is not None

        entry = lumio_wiki.make_activity_log_entry(
            operation="verify",
            description="isolated wheel publication",
        )
        log_path = lumio_wiki.append_activity_log_entry(regenerated, entry)
        assert log_path == regenerated / "log.md"
        assert "verify: isolated wheel publication" in log_path.read_text(encoding="utf-8")

        exported = lumio_wiki.export_okf_profile1(kb.public_pages())
        bundle = Path(temporary) / "okf"
        for relative, content in exported.files.items():
            target = bundle / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        assert lumio_wiki.import_okf_profile1(bundle).proposed_pages

    imported_forbidden = {
        name.split(".", 1)[0] for name in sys.modules if name.split(".", 1)[0] in FORBIDDEN
    }
    assert not imported_forbidden, sorted(imported_forbidden)
    print("lumio-wiki isolated wheel verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
