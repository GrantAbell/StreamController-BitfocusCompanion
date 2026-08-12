"""Localization coverage.

Checks mechanically that every key the code asks for exists in `locales.csv`,
so a renamed key surfaces as a test failure rather than as raw key text
appearing in the UI.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
LOCALES = ROOT / "locales.csv"

# Files that build user-visible UI.
UI_SOURCES = [
    ROOT / "actions" / "companion_button.py",
    ROOT / "actions" / "companion_page.py",
    ROOT / "ui" / "plugin_settings.py",
    ROOT / "main.py",
]

KEY_PATTERN = re.compile(r'_text\(\s*"([a-z0-9._-]+)"|\.get\(\s*"([a-z0-9._-]+)"')


def _load_locales() -> dict[str, dict[str, str]]:
    with LOCALES.open(newline="") as handle:
        reader = csv.reader(handle, delimiter=";", quotechar='"', skipinitialspace=True)
        languages = next(reader)[1:]
        data = {}
        for row in reader:
            if not row:
                continue
            data[row[0]] = dict(zip(languages, row[1:]))
    return data


def _referenced_keys() -> set[str]:
    keys: set[str] = set()
    for path in UI_SOURCES:
        if not path.exists():
            continue
        for match in KEY_PATTERN.finditer(path.read_text()):
            key = match.group(1) or match.group(2)
            # Settings dict lookups share the .get( shape; locale keys always
            # contain a dot, settings keys never do.
            if key and "." in key:
                keys.add(key)
    return keys


def test_locales_file_exists():
    assert LOCALES.exists(), "locales.csv is required by the modern LocaleManager"


def test_english_is_present():
    with LOCALES.open(newline="") as handle:
        header = next(csv.reader(handle, delimiter=";"))
    assert header[0] == "key"
    assert "en_US" in header


def test_every_entry_has_an_english_string():
    for key, translations in _load_locales().items():
        assert translations.get("en_US"), f"{key} has no en_US translation"


@pytest.mark.parametrize("key", sorted(_referenced_keys()))
def test_referenced_key_is_defined(key):
    assert key in _load_locales(), (
        f"{key} is used in the UI but missing from locales.csv"
    )


def test_no_duplicate_keys():
    with LOCALES.open(newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        next(reader)
        keys = [row[0] for row in reader if row]

    duplicates = {key for key in keys if keys.count(key) > 1}
    assert not duplicates, f"duplicate locale keys: {duplicates}"


def test_multiline_key_labels_use_escaped_newlines():
    """The loader turns a literal backslash-n into a real newline."""
    locales = _load_locales()
    assert "\\n" in locales["key.offline"]["en_US"]


def test_strings_avoid_apostrophes():
    """LocaleManager HTML-escapes results, so an apostrophe would render as
    &#x27; on a key label. Phrasing avoids them instead."""
    offenders = [
        key
        for key, translations in _load_locales().items()
        if key.startswith("key.") and "'" in translations.get("en_US", "")
    ]
    assert not offenders, f"key labels must avoid apostrophes: {offenders}"
