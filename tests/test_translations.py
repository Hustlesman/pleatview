"""The integration's translations match the English: same strings, same placeholders."""

import json
import re
from pathlib import Path

import pytest

TRANSLATIONS = Path(__file__).parent.parent / "custom_components" / "pleatview" / "translations"


def _strings(path: Path) -> dict[str, str]:
    def walk(node: dict, prefix: str = ""):
        for key, value in node.items():
            if isinstance(value, dict):
                yield from walk(value, f"{prefix}{key}.")
            else:
                yield f"{prefix}{key}", value

    return dict(walk(json.loads(path.read_text(encoding="utf-8"))))


ENGLISH = _strings(TRANSLATIONS / "en.json")
OTHERS = sorted(p for p in TRANSLATIONS.glob("*.json") if p.name != "en.json")


def test_there_are_translations() -> None:
    assert len(OTHERS) >= 10


@pytest.mark.parametrize("path", OTHERS, ids=lambda p: p.stem)
def test_translation_matches_english(path: Path) -> None:
    strings = _strings(path)
    assert sorted(strings) == sorted(ENGLISH)
    for key, text in strings.items():
        assert text.strip(), key
        assert sorted(re.findall(r"\{\w+\}", text)) == sorted(re.findall(r"\{\w+\}", ENGLISH[key])), key
