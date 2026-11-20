"""Generic loader shim for a Nuitka-compiled SakuraMedia plugin.

This file is copied into the plugin zip as ``__init__.py``. It keeps the
host-facing package a normal Python package while the real implementation
lives in ``<plugin_id><EXT_SUFFIX>`` next to this file.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import sysconfig
from pathlib import Path

_PLUGIN_ID = __name__.rsplit(".", 1)[-1]
_HERE = Path(__file__).resolve().parent
_SUFFIX = sysconfig.get_config_var("EXT_SUFFIX") or ""
_BINARY = _HERE / f"{_PLUGIN_ID}{_SUFFIX}"

if not _BINARY.is_file():
    raise ImportError(
        f"{_PLUGIN_ID}: compiled binary not found: {_BINARY.name} "
        f"(platform={sys.platform}, suffix={_SUFFIX})"
    )


def _load():
    # Preferred: import as a submodule of the host package. This keeps the
    # module inside ``sakuramedia_plugins.<plugin_id>`` so the host's
    # ``_clear_plugin_modules`` can evict it on reload/upgrade.
    try:
        return importlib.import_module(f".{_PLUGIN_ID}", __name__)
    except ImportError:
        # Fallback for a Nuitka build that does not tolerate the nested name.
        # Load the same binary as a top-level module and evict a stale copy
        # from a previous load first.
        for name in [
            item
            for item in sys.modules
            if item == _PLUGIN_ID or item.startswith(_PLUGIN_ID + ".")
        ]:
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location(_PLUGIN_ID, _BINARY)
        if spec is None or spec.loader is None:
            raise
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PLUGIN_ID] = module
        spec.loader.exec_module(module)
        return module


_impl = _load()


def __getattr__(name: str):
    return getattr(_impl, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_impl)))
