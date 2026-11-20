"""Load a compiled plugin inside the backend image and check its registration."""

import sys
from pathlib import Path

from pydantic import BaseModel

from src.config.config import Plugins
from src.plugins.loader import (
    PLUGIN_LOAD_ERRORS,
    check_plugin_dir,
    load_enabled_plugins,
    load_plugin_settings_model,
)
from src.plugins.manifest import load_manifest_from_file

plugin_ids = sys.argv[1:]
root = Path("/poc/plugins")

for plugin_id in plugin_ids:
    plugin_dir = root / plugin_id
    manifest = load_manifest_from_file(plugin_dir)
    print(
        f"[{plugin_id}] manifest version={manifest.version} "
        f"host_api={manifest.host_api_version} settings_model={manifest.settings_model}"
    )
    settings_model = load_plugin_settings_model(plugin_dir)
    is_host_model = isinstance(settings_model, type) and issubclass(
        settings_model, BaseModel
    )
    print(f"[{plugin_id}] settings_model={settings_model} pydantic={is_host_model}")
    registration = check_plugin_dir(plugin_dir=plugin_dir)
    print(
        f"[{plugin_id}] check version={registration.version} "
        f"display_name_i18n_keys={sorted(registration.display_name_i18n)}"
    )

registrations = load_enabled_plugins(Plugins(enabled=plugin_ids), root_dir=root)
print(
    "loaded:",
    [(r.plugin_id, r.version, sorted(r.display_name_i18n)) for r in registrations],
)
print("errors:", PLUGIN_LOAD_ERRORS)
assert len(registrations) == len(plugin_ids), registrations
assert not PLUGIN_LOAD_ERRORS, PLUGIN_LOAD_ERRORS
for registration in registrations:
    manifest = load_manifest_from_file(root / registration.plugin_id)
    assert registration.version == manifest.version
    assert registration.host_api_version == manifest.host_api_version
print("VERIFY_OK")
