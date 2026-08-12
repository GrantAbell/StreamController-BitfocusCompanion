"""Enforces the rule that `companion/` never depends on StreamController.

That boundary is what lets the protocol, domain and transport code be tested
without the host application. It is easy to break by
reflex — StreamController plugins normally use `loguru`, which is not
installed outside the Flatpak — so it is checked mechanically rather than by
discipline.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

import companion

PACKAGE_ROOT = Path(companion.__file__).parent

# Imports that only exist inside the StreamController runtime.
FORBIDDEN_ROOTS = {"src", "gi", "GtkHelper", "globals", "loguru", "rpyc"}


def _module_names() -> list[str]:
    return [
        name
        for _finder, name, _ispkg in pkgutil.walk_packages(
            companion.__path__, prefix="companion."
        )
    ]


def _source_files() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


@pytest.mark.parametrize("module_name", _module_names())
def test_every_module_imports_without_streamcontroller(module_name):
    """The suite itself proves this, but naming the module pinpoints failures."""
    importlib.import_module(module_name)


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
def test_no_streamcontroller_imports(path: Path):
    tree = ast.parse(path.read_text(), filename=str(path))

    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_ROOTS:
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import, always within this package.
            if node.level == 0 and node.module:
                if node.module.split(".")[0] in FORBIDDEN_ROOTS:
                    offenders.append(node.module)

    assert not offenders, (
        f"{path.name} imports {offenders}, which only exist inside "
        f"StreamController. Use stdlib `logging` instead of `loguru`, and keep "
        f"host APIs in main.py / actions/."
    )


def test_pillow_is_the_only_expected_third_party_dependency():
    """Phase 1 declares no dependencies beyond what StreamController provides."""
    requirements = (PACKAGE_ROOT.parent / "requirements.txt").read_text()
    declared = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert declared == [], f"unexpected declared dependencies: {declared}"
