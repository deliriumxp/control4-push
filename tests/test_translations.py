"""Пользовательская интеграция читает только translations/en.json, и ссылки [%key:…%] там не разворачиваются."""

import json
from pathlib import Path

INTEGRATION = Path(__file__).parents[1] / "custom_components" / "control4"


def test_переводы_развёрнуты_и_совпадают_со_strings():
    strings = (INTEGRATION / "strings.json").read_text()
    english = (INTEGRATION / "translations" / "en.json").read_text()

    assert "[%key:" not in english
    assert json.loads(english) == json.loads(strings)
