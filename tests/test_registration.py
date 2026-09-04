from __future__ import annotations

import json
from pathlib import Path

from sakuramedia_local_provider.plugin import DISPLAY_NAME, PLUGIN_ID, register

from src.plugins import PluginContext
from src.plugins.provider_protocol import MEDIA_PROVIDER_EXTENSION_KEY


def test_manifest_and_registration_declare_host_api_v5(tmp_path: Path) -> None:
    manifest = json.loads((Path(__file__).parents[1] / "manifest.json").read_text(encoding="utf-8"))
    registration = register(
        PluginContext(plugin_id=PLUGIN_ID, settings={}, data_dir=tmp_path / "plugin-data")
    )
    assert manifest["plugin_id"] == PLUGIN_ID
    assert manifest["display_name"] == DISPLAY_NAME
    assert manifest["host_api_version"] == 5
    assert manifest["dependencies"] == [
        "qbittorrent-api>=2026.8.1",
        "libtorrent==2.0.9; sys_platform == 'darwin' and platform_machine == 'x86_64'",
        "libtorrent>=2.1.1,<3.0.0; sys_platform != 'darwin' or platform_machine == 'arm64'",
    ]
    assert registration.plugin_id == PLUGIN_ID
    assert registration.display_name == DISPLAY_NAME
    assert registration.host_api_version == 5
    assert [extension.key for extension in registration.extensions] == [MEDIA_PROVIDER_EXTENSION_KEY]
    assert registration.extensions[0].data.playback_deliveries == ("proxy",)


def test_library_configuration_is_normalised_without_creating_directories(tmp_path: Path) -> None:
    registration = register(
        PluginContext(plugin_id=PLUGIN_ID, settings={}, data_dir=tmp_path / "plugin-data")
    )
    bundle = registration.extensions[0].data
    media_root = tmp_path / "media"
    manual_import_root = tmp_path / "imports"
    prepared = bundle.prepare_library(
        submitted_config={
            "media_root_path": str(media_root),
            "manual_import_root_path": str(manual_import_root),
        },
        previous=None,
    )
    assert prepared.provider_config == {
        "media_root_path": str(media_root),
        "manual_import_root_path": str(manual_import_root),
    }
    assert not media_root.exists()
    assert not manual_import_root.exists()


def test_registration_preserves_plugin_data_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "plugin-data"
    registration = register(
        PluginContext(plugin_id=PLUGIN_ID, settings={}, data_dir=data_dir)
    )
    bundle = registration.extensions[0].data
    assert bundle.data_dir == data_dir
