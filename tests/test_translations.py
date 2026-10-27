from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path

import pytest
from sakuramedia_local_provider import translations
from sakuramedia_local_provider.cleanup import cleanup_empty_media_dirs
from sakuramedia_local_provider.plugin import (
    DISPLAY_NAME,
    LIBRARY_CONFIG_FIELDS,
    PLUGIN_ID,
    register,
)
from sakuramedia_local_provider.qbittorrent import QbittorrentDownloadComponent
from sakuramedia_local_provider.storage import LocalStorageProvider

from src.i18n.locale import LanguageState, reset_language_state, set_language_state
from src.plugins import PluginContext
from src.plugins.i18n import PluginI18n
from src.plugins.provider_protocol import LibraryHandle, ProviderOperationError

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@contextmanager
def _language(locale: str):
    token = set_language_state(LanguageState(header_language=locale))
    try:
        yield
    finally:
        reset_language_state(token)


def _catalogs() -> tuple[dict[str, str], dict[str, str]]:
    root = PLUGIN_ROOT / "i18n"
    return (
        json.loads((root / "zh-CN.json").read_text(encoding="utf-8")),
        json.loads((root / "en.json").read_text(encoding="utf-8")),
    )


def _library(tmp_path: Path, *, provider_config: object) -> LibraryHandle:
    return LibraryHandle(
        library_id=1,
        provider_key="local",
        provider_config=provider_config,
        account_key=None,
    )


def _valid_config(tmp_path: Path) -> dict[str, str]:
    return {
        "media_root_path": str(tmp_path / "media"),
        "manual_import_root_path": str(tmp_path / "imports"),
    }


def test_catalogs_share_keys_and_placeholders() -> None:
    zh, en = _catalogs()
    assert set(zh) == set(en)
    mismatched = {
        key: (_PLACEHOLDER.findall(zh[key]), _PLACEHOLDER.findall(en[key]))
        for key in zh
        if set(_PLACEHOLDER.findall(zh[key])) != set(_PLACEHOLDER.findall(en[key]))
    }
    assert mismatched == {}
    assert zh["manifest.display_name"] == DISPLAY_NAME
    assert en["manifest.display_name"] == "Local Storage & qBittorrent"


def test_text_helpers_fall_back_when_keys_are_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        translations,
        "_CATALOGS",
        {"zh-CN": {"only.zh": "中文"}, "en": {}},
    )

    assert translations.text("only.zh") == "中文"
    assert translations.text("only.zh", locale="en") == "中文"
    assert translations.text("missing", default="fallback") == "fallback"
    assert translations.text("missing") == "missing"
    assert translations.text_map("only.zh") == {"zh-CN": "中文"}
    assert translations.text_map("missing") == {}


def test_translator_without_host_i18n_returns_source_text() -> None:
    translator = translations.Translator()

    assert translator.t("manifest.display_name") == DISPLAY_NAME
    assert translator.t("storage.progress.thumbnails", done=1, total=3) == (
        "正在生成缩略图 · 已生成 1/3 张"
    )


def test_translator_with_unloaded_host_catalogs_keeps_formatted_source() -> None:
    # 宿主没有插件目录时语言包为空，同样要拿到插值后的中文兜底。
    translator = translations.Translator(PluginI18n(PLUGIN_ID))

    assert translator.t("storage.progress.thumbnails", done=2, total=3) == (
        "正在生成缩略图 · 已生成 2/3 张"
    )


def test_static_declarations_expose_both_languages(tmp_path: Path) -> None:
    registration = register(
        PluginContext(plugin_id=PLUGIN_ID, settings={}, data_dir=tmp_path / "plugin-data")
    )
    bundle = registration.extensions[0].data

    assert registration.display_name_i18n == {
        "zh-CN": "本地存储与 qBittorrent",
        "en": "Local Storage & qBittorrent",
    }
    assert bundle.display_name_i18n == registration.display_name_i18n
    assert registration.jobs[0].cli_help_i18n == {
        "zh-CN": "清理本地媒体库空目录（请在无导入、转移任务时运行）",
        "en": "Clean up empty local media library directories (run while no import or transfer task is active)",
    }
    media_root_field = LIBRARY_CONFIG_FIELDS[0]
    assert media_root_field.label == "媒体库路径"
    assert media_root_field.label_i18n == {
        "zh-CN": "媒体库路径",
        "en": "Media library path",
    }
    assert media_root_field.hint_i18n == {"zh-CN": "例如: /mnt/media", "en": "e.g. /mnt/media"}

    download_fields = {
        field.key: field for field in QbittorrentDownloadComponent(data_dir=tmp_path / "data").config_fields
    }
    assert download_fields["base_url"].label == "qBittorrent 地址"
    assert download_fields["base_url"].label_i18n == {
        "zh-CN": "qBittorrent 地址",
        "en": "qBittorrent address",
    }
    assert download_fields["password"].description_i18n == {
        "zh-CN": "qBittorrent WebUI 登录密码；编辑时留空表示保留已保存密码。",
        "en": "qBittorrent WebUI login password; leave empty while editing to keep the saved password.",
    }


def test_provider_error_message_follows_current_language(tmp_path: Path) -> None:
    i18n = PluginI18n(PLUGIN_ID, PLUGIN_ROOT)
    library = _library(tmp_path, provider_config=None)

    with _language("zh-CN"), pytest.raises(ProviderOperationError) as zh_error:
        LocalStorageProvider(
            library=library, data_dir=tmp_path / "plugin-data", i18n=i18n
        )
    with _language("en"), pytest.raises(ProviderOperationError) as en_error:
        LocalStorageProvider(
            library=library, data_dir=tmp_path / "plugin-data", i18n=i18n
        )

    assert zh_error.value.code == en_error.value.code == "invalid_config"
    assert zh_error.value.safe_message == "本地存储配置无效"
    assert en_error.value.safe_message == "Invalid local storage configuration"


def test_scan_progress_follows_current_language(tmp_path: Path) -> None:
    i18n = PluginI18n(PLUGIN_ID, PLUGIN_ROOT)
    library = _library(tmp_path, provider_config=_valid_config(tmp_path))
    imports = tmp_path / "imports"
    imports.mkdir(parents=True)
    (imports / "clip.mp4").write_bytes(b"video")

    def scan() -> list[dict]:
        provider = LocalStorageProvider(
            library=library, data_dir=tmp_path / "plugin-data", i18n=i18n
        )
        progress: list[dict] = []
        provider.scan_import_source(
            source_ref={"version": 1, "kind": "manual_local_path", "relative_path": ""},
            progress_callback=progress.append,
        )
        return progress

    with _language("zh-CN"):
        zh_progress = scan()
    with _language("en"):
        en_progress = scan()

    assert zh_progress[0]["text"] == "枚举本地目录 · 已检查 0 个条目"
    assert en_progress[0]["text"] == "Enumerating local directories · checked 0 entries"
    assert zh_progress[-1]["text"] == "本地扫描完成 · 发现 1 个文件"
    assert en_progress[-1]["text"] == "Local scan complete · files found: 1"


def test_cleanup_progress_follows_current_language(tmp_path: Path, monkeypatch) -> None:
    media_root = tmp_path / "media"
    (media_root / "jav/empty").mkdir(parents=True)
    libraries = [
        type("Library", (), {"id": 1, "provider_config": {"media_root_path": str(media_root)}})
    ]
    monkeypatch.setattr(
        "sakuramedia_local_provider.cleanup._load_media_libraries", lambda: libraries
    )
    i18n = PluginI18n(PLUGIN_ID, PLUGIN_ROOT)

    class Reporter:
        def __init__(self) -> None:
            self.updates: list[dict] = []

        def progress_callback(self, payload: dict) -> None:
            self.updates.append(payload)

    with _language("zh-CN"):
        zh_reporter = Reporter()
        cleanup_empty_media_dirs(zh_reporter, {}, i18n=i18n)
    (media_root / "jav/empty").mkdir()
    with _language("en"):
        en_reporter = Reporter()
        cleanup_empty_media_dirs(en_reporter, {}, i18n=i18n)

    zh_texts = [update["text"] for update in zh_reporter.updates]
    en_texts = [update["text"] for update in en_reporter.updates]
    assert any("阶段 1/2 · 扫描目录" in text for text in zh_texts)
    assert any("Stage 1/2 · scanning directories" in text for text in en_texts)
    assert zh_texts[-1].endswith("扫描错误 0")
    assert en_texts[-1].endswith("scan errors 0")
