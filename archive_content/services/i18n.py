from __future__ import annotations

import json
from html import escape
from functools import lru_cache
from pathlib import Path
from typing import Any

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
SUPPORTED = ("uk", "en", "es", "fr", "de", "zh", "ru")
DEFAULT = "uk"


class SafeHtml(str):
    """Explicitly trusted HTML fragment used only for generated Telegram markup."""
    pass


@lru_cache(maxsize=None)
def _bundle(lang: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{lang}.json"
    if not path.exists():
        path = LOCALES_DIR / f"{DEFAULT}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def t(lang: str, key: str, **kwargs: Any) -> str:
    lang = lang if lang in SUPPORTED else DEFAULT
    bundle = _bundle(lang)
    template = bundle.get(key) or _bundle(DEFAULT).get(key) or key
    try:
        safe_kwargs = {k: (str(v) if isinstance(v, SafeHtml) else escape(str(v), quote=True)) for k, v in kwargs.items()}
        return template.format(**safe_kwargs) if kwargs else template
    except (KeyError, IndexError):
        return template


def normalize_lang(code: str | None) -> str:
    if not code:
        return DEFAULT
    code = code.lower().split("-")[0]
    return code if code in SUPPORTED else DEFAULT
