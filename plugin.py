"""Host API v4 bundle registration for local media storage."""

from __future__ import annotations

import json
from pathlib import Path

from src.plugins import (
    HOST_API_VERSION,
    PluginContext,
    PluginExtension,
    PluginRegistration,
)
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_EXTENSION_KEY,
    ConfigField,
    JsonObject,
    LibraryHandle,
    PreparedLibrary,
    ProviderOperationError,
)

from .qbittorrent import QbittorrentDownloadComponent
from .storage import LocalStorageProvider, _reject_symlink_components

PLUGIN_ID = "sakuramedia_local_provider"
DISPLAY_NAME = "本地存储与 qBittorrent"
VERSION = json.loads(
    Path(__file__).with_name("manifest.json").read_text(encoding="utf-8")
)["version"]

LIBRARY_CONFIG_FIELDS = (
    ConfigField(
        key="media_root_path",
        label="媒体库路径",
        input="path",
        required=True,
        description="媒体文件导入后的最终存放目录,填绝对路径",
        hint="例如: /mnt/media",
    ),
    ConfigField(
        key="manual_import_root_path",
        label="手动导入根目录",
        input="path",
        required=True,
        description="手动浏览和导入本地文件时使用的目录；用于导入已有媒体。",
        hint="/mnt",
    ),
    ConfigField(
        key="filename_blacklist",
        label="文件名黑名单",
        input="text",
        required=False,
        description="每行一个关键字；文件名包含关键字时不会导入，匹配不区分大小写。",
        multiline=True,
        hint="sample\ntrailer",
    ),
)


def _normalise_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("invalid path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    _reject_symlink_components(path)
    return str(path.resolve(strict=False))


def _normalise_filename_blacklist(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("invalid filename blacklist")
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


class LocalMediaProviderBundle:
    provider_key = "local"
    display_name = DISPLAY_NAME
    library_config_fields = LIBRARY_CONFIG_FIELDS
    playback_deliveries = ("proxy",)
    merged_playback_format = "mp4"

    def __init__(self, *, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.downloads = QbittorrentDownloadComponent(data_dir=data_dir)

    def prepare_library(
        self,
        *,
        submitted_config: JsonObject,
        previous: LibraryHandle | None,
    ) -> PreparedLibrary:
        if not isinstance(submitted_config, dict):
            raise ProviderOperationError(
                provider_key=self.provider_key,
                operation="prepare_library",
                code="invalid_config",
                safe_message="本地存储配置无效",
                retryable=False,
            )
        required_fields = {"media_root_path", "manual_import_root_path"}
        allowed_fields = required_fields | {"filename_blacklist"}
        if not required_fields <= set(submitted_config) or not set(submitted_config) <= allowed_fields:
            raise ProviderOperationError(
                provider_key=self.provider_key,
                operation="prepare_library",
                code="invalid_config",
                safe_message="本地存储配置字段无效",
                retryable=False,
            )
        try:
            config = {
                "media_root_path": _normalise_path(submitted_config["media_root_path"]),
                "manual_import_root_path": _normalise_path(
                    submitted_config["manual_import_root_path"]
                ),
                "filename_blacklist": _normalise_filename_blacklist(
                    submitted_config.get("filename_blacklist")
                ),
            }
        except (OSError, ValueError) as exc:
            raise ProviderOperationError(
                provider_key=self.provider_key,
                operation="prepare_library",
                code="invalid_config",
                safe_message="本地存储路径配置无效",
                retryable=False,
            ) from exc
        return PreparedLibrary(provider_config=config, account_key=None)

    def build_storage(self, *, library: LibraryHandle) -> LocalStorageProvider:
        return LocalStorageProvider(library=library, data_dir=self.data_dir)


def register(context: PluginContext) -> PluginRegistration:
    """Declare the provider without touching configured library paths."""
    bundle = LocalMediaProviderBundle(data_dir=context.data_dir)
    return PluginRegistration(
        plugin_id=PLUGIN_ID,
        display_name=DISPLAY_NAME,
        version=VERSION,
        host_api_version=HOST_API_VERSION,
        extensions=(
            PluginExtension(key=MEDIA_PROVIDER_EXTENSION_KEY, data=bundle),
        ),
    )
