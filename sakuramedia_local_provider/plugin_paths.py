"""Locate the installed plugin root for source and compiled layouts."""

from __future__ import annotations

from pathlib import Path


def plugin_root() -> Path:
    """Return the directory that contains this plugin's ``manifest.json``.

    Nuitka-compiled packages report ``__file__`` one level below the
    installed plugin root (``<root>/<plugin_id>/...``), so walk upwards
    until the manifest is found.
    """

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "manifest.json").is_file():
            return parent
    return here.parent
