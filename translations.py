"""Load bundled plugin translations for static ``*_i18n`` fields.

The host treats missing translations as optional.  Follow the same rule here so
one missing catalog or key cannot take down the whole plugin at import time.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_LOCALES = ("zh-CN", "en")
_DIR = Path(__file__).with_name("i18n")
_logger = logging.getLogger(__name__)


def _load_catalog(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _logger.warning("plugin i18n catalog missing: %s", path)
        return {}
    except (OSError, json.JSONDecodeError):
        _logger.exception("plugin i18n catalog invalid: %s", path)
        return {}
    if not isinstance(data, dict):
        _logger.error("plugin i18n catalog root must be an object: %s", path)
        return {}
    return {
        key: value
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, str)
    }


_CATALOGS = {
    locale: _load_catalog(_DIR / f"{locale}.json")
    for locale in _LOCALES
}


def text_map(key: str) -> dict[str, str]:
    """Return only the locales that actually define ``key``."""
    return {
        locale: _CATALOGS[locale][key]
        for locale in _LOCALES
        if key in _CATALOGS.get(locale, {})
    }


def text(key: str, locale: str = "zh-CN", default: str | None = None) -> str:
    """Return the best available source text; never fail on a missing key."""
    for candidate in (locale, "zh-CN", "en"):
        value = _CATALOGS.get(candidate, {}).get(key)
        if value is not None:
            return value
    return default if default is not None else key


class Translator:
    """Resolve runtime text (progress, provider errors, diagnostics) lazily.

    ``i18n`` is the host ``PluginI18n``; it resolves the language of the current
    request or task on every call, so one instance may be shared for the plugin
    lifetime.  Without it the Chinese source text is returned, which keeps the
    default behaviour identical outside a host request.
    """

    def __init__(self, i18n: Any | None = None) -> None:
        self.i18n = i18n

    def t(self, key: str, **params: Any) -> str:
        source = text(key)
        default = source.format(**params) if params else source
        if self.i18n is not None:
            return self.i18n.t(key, default=default, **params)
        return default
