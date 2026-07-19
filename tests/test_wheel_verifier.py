from __future__ import annotations

import zipfile
from pathlib import Path

from scripts.verify_lumio_wiki_wheel import _python_roots


def test_python_roots_includes_packages_top_level_modules_and_extensions(
    tmp_path: Path,
):
    wheel = tmp_path / "example.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("package/__init__.py", "")
        archive.writestr("native_package/_speedups.cpython-314-x86_64-linux-gnu.so", "")
        archive.writestr("module.py", "")
        archive.writestr("extension.cpython-314-x86_64-linux-gnu.so", "")
        archive.writestr("windows_extension.cp314-win_amd64.pyd", "")
        archive.writestr("example-1.0.dist-info/metadata.py", "")
        archive.writestr("example-1.0.data/scripts/helper.py", "")

    assert _python_roots(wheel) == {
        "extension",
        "module",
        "native_package",
        "package",
        "windows_extension",
    }


def test_python_roots_includes_relocated_purelib_and_platlib_modules(
    tmp_path: Path,
):
    wheel = tmp_path / "example.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("example-1.0.data/purelib/pure_package/__init__.py", "")
        archive.writestr("example-1.0.data/purelib/pure_module.py", "")
        archive.writestr(
            "example-1.0.data/platlib/platform_extension.cp314-win_amd64.pyd",
            "",
        )
        archive.writestr("example-1.0.data/scripts/helper.py", "")
        archive.writestr("example-1.0.data/headers/generated.py", "")
        archive.writestr("example-1.0.data/data/templates/example.py", "")

    assert _python_roots(wheel) == {
        "platform_extension",
        "pure_module",
        "pure_package",
    }
