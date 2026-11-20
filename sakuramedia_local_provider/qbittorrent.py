"""qBittorrent download component for the local provider bundle.

Only the provider protocol is imported from the host. Completed tasks carry a
provider-owned relative path and the backend-side download root so the local
storage provider can scan the same directory from the backend process.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import posixpath
import re
import secrets
import shutil
import time
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from src.plugins.provider_protocol import (
    ConfigField,
    DownloadClientHandle,
    DownloadSubmission,
    JsonObject,
    LibraryHandle,
    ProviderDiagnosticCheck,
    ProviderDiagnosticReport,
    ProviderOperationError,
    RemoteDownloadTask,
)

from .storage import _reject_symlink_components, _root_path
from .translations import Translator, text_map

SYSTEM_TAG = "sakuramedia"
CLIENT_TAG_PREFIX = "client:"
LOCAL_REF_VERSION = 1
LOCAL_REF_KIND = "download_local_path"
MAX_TORRENT_BYTES = 16 * 1024 * 1024
MAX_HTTP_REDIRECTS = 5
DEAD_TORRENT_IDLE_SECONDS = 24 * 60 * 60
_BTIH_RE = re.compile(r"urn:btih:([A-Za-z0-9]+)", re.IGNORECASE)
_ALLOWED_CONFIG_KEYS = frozenset(
    {"base_url", "username", "password", "remote_save_root", "backend_import_root_path"}
)
logger = logging.getLogger(__name__)


def _diagnostic_report(checks: list[ProviderDiagnosticCheck]) -> ProviderDiagnosticReport:
    if any(check.status == "failed" for check in checks):
        status = "failed"
    elif any(check.status == "warning" for check in checks):
        status = "warning"
    else:
        status = "ok"
    return ProviderDiagnosticReport(status=status, checks=tuple(checks))


_ErrorCode = Literal[
    "invalid_config",
    "authentication_failed",
    "source_not_found",
    "task_not_managed",
    "source_blacklisted",
    "unsupported",
    "unavailable",
]


class _TranslatedErrors:
    """Build locale-aware ``ProviderOperationError`` values from catalog keys."""

    translations: Translator

    def _error(
        self,
        operation: str,
        code: _ErrorCode,
        key: str,
        *,
        retryable: bool = False,
    ) -> ProviderOperationError:
        return ProviderOperationError(
            provider_key="local",
            operation=operation,
            code=code,
            safe_message=self.translations.t(key),
            retryable=retryable,
        )


def _value(item: object, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _directory_entry_name(entry: object) -> str:
    if isinstance(entry, str):
        raw_name = entry
    elif isinstance(entry, Mapping):
        raw_name = entry.get("name") or entry.get("path") or ""
    else:
        raw_name = getattr(entry, "name", None) or getattr(entry, "path", "")
    text = str(raw_name or "").strip()
    if not text:
        return ""
    return text.replace("\\", "/").rstrip("/").split("/")[-1]


def _tags(value: object) -> set[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        values = str(value or "").split(",")
    return {str(item).strip() for item in values if str(item).strip()}


def _managed(tags: object, client_id: int) -> bool:
    parsed = _tags(tags)
    return SYSTEM_TAG in parsed and f"{CLIENT_TAG_PREFIX}{client_id}" in parsed


def canonical_btih(value: str) -> str:
    """Return a lower-case 40-character hexadecimal BitTorrent info hash."""
    text = value.strip()
    if len(text) == 40:
        try:
            bytes.fromhex(text)
        except ValueError as exc:
            raise ValueError("invalid btih") from exc
        return text.lower()
    if len(text) != 32 or not re.fullmatch(r"[A-Za-z2-7]+", text):
        raise ValueError("invalid btih")
    try:
        return base64.b32decode(text.upper() + "=" * ((8 - len(text) % 8) % 8)).hex()
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid btih") from exc


def parse_hash_from_magnet(value: str) -> str:
    match = _BTIH_RE.search(unquote(value))
    if match is None:
        raise ValueError("invalid magnet")
    return canonical_btih(match.group(1))


def _is_magnet(value: str) -> bool:
    return value.strip().lower().startswith("magnet:")


def _safe_display_name(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("invalid display name")
    name = " ".join(value.replace("\x00", " ").replace("/", " ").replace("\\", " ").split())
    if not name or name in {".", ".."}:
        raise ValueError("invalid display name")
    return name


def _normalise_base_url(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("invalid base url")
    text = value.strip().rstrip("/")
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("invalid base url")
    if parsed.username or parsed.password:
        raise ValueError("invalid base url")
    return text


def _normalise_remote_root(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("invalid remote root")
    text = value.strip()
    if not text.startswith("/") or "\x00" in text or "\\" in text:
        raise ValueError("invalid remote root")
    if any(part == ".." for part in text.split("/")):
        raise ValueError("invalid remote root")
    normalized = posixpath.normpath("/" + text.lstrip("/"))
    parts = tuple(part for part in normalized.split("/") if part)
    if normalized == "/" or not parts or any(part in {".", ".."} for part in parts):
        raise ValueError("invalid remote root")
    return normalized


def _remote_save_path(root: str, display_name: str) -> str:
    return posixpath.join(root, display_name)


def map_qb_state(raw_state: object) -> str:
    """Collapse qB's detailed states into the four protocol states."""
    state = str(raw_state or "").strip()
    if state in {
        "uploading",
        "stalledUP",
        "queuedUP",
        "forcedUP",
        "pausedUP",
        "stoppedUP",
        "checkingUP",
    }:
        return "completed"
    if state in {"error", "missingFiles"}:
        return "failed"
    if state in {"queuedDL", "pausedDL", "stoppedDL"}:
        return "queued"
    if state in {
        "downloading",
        "metaDL",
        "forcedDL",
        "allocating",
        "stalledDL",
        "checkingDL",
    }:
        return "downloading"
    return "queued"


def _progress(value: object, *, completed: bool) -> float:
    try:
        current = float(value or 0)
    except (TypeError, ValueError):
        current = 0.0
    if completed:
        return 1.0
    return min(1.0, max(0.0, current))


def _torrent_hash(item: object) -> str:
    value = _value(item, "hash", _value(item, "info_hash", ""))
    text = str(value or "").strip()
    return canonical_btih(text) if text else ""


class QbittorrentDownloadComponent(_TranslatedErrors):
    """Declaration and construction boundary for qBittorrent downloads."""

    supports_torrent_bytes = True

    def __init__(self, *, data_dir: Path, i18n: object | None = None) -> None:
        self.data_dir = data_dir
        self.translations = Translator(i18n)

    config_fields = (
        ConfigField(
            key="base_url",
            label="qBittorrent 地址",
            input="text",
            required=True,
            description="qBittorrent WebUI 地址，后端会使用它进行连接测试和提交下载任务。",
            hint="例如 https://qb.example.com",
            label_i18n=text_map("settings.base_url.label"),
            description_i18n=text_map("settings.base_url.description"),
            hint_i18n=text_map("settings.base_url.hint"),
        ),
        ConfigField(
            key="username",
            label="用户名",
            input="text",
            required=True,
            description="qBittorrent WebUI 登录用户名。",
            label_i18n=text_map("settings.username.label"),
            description_i18n=text_map("settings.username.description"),
        ),
        ConfigField(
            key="password",
            label="密码",
            input="secret",
            required=True,
            description="qBittorrent WebUI 登录密码；编辑时留空表示保留已保存密码。",
            label_i18n=text_map("settings.password.label"),
            description_i18n=text_map("settings.password.description"),
        ),
        ConfigField(
            key="remote_save_root",
            label="qBittorrent 保存根目录",
            input="path",
            required=True,
            description="qBittorrent 进程看到的下载根目录，例如 /downloads。",
            hint="例如 /downloads",
            label_i18n=text_map("settings.remote_save_root.label"),
            description_i18n=text_map("settings.remote_save_root.description"),
            hint_i18n=text_map("settings.remote_save_root.hint"),
        ),
        ConfigField(
            key="backend_import_root_path",
            label="后端下载根目录",
            input="path",
            required=True,
            description="后端进程看到的同一下载根目录；必须与 qB 路径映射到同一个宿主机目录。",
            hint="例如 /mnt/qb-downloads",
            label_i18n=text_map("settings.backend_import_root_path.label"),
            description_i18n=text_map("settings.backend_import_root_path.description"),
            hint_i18n=text_map("settings.backend_import_root_path.hint"),
        ),
    )

    def prepare_client(
        self,
        *,
        submitted_config: JsonObject,
        library: LibraryHandle,
        previous: DownloadClientHandle | None,
    ) -> JsonObject:
        del library, previous
        if not isinstance(submitted_config, dict) or set(submitted_config) != _ALLOWED_CONFIG_KEYS:
            raise self._error("prepare_client", "invalid_config", "qb.error.invalid_config_fields")
        try:
            base_url = _normalise_base_url(submitted_config["base_url"])
            username = submitted_config["username"]
            password = submitted_config["password"]
            if not isinstance(username, str) or not username.strip():
                raise ValueError("invalid username")
            if not isinstance(password, str) or not password:
                raise ValueError("invalid password")
            remote_save_root = _normalise_remote_root(submitted_config["remote_save_root"])
            backend_import_root_path = str(_root_path(submitted_config["backend_import_root_path"]))
        except (OSError, TypeError, ValueError) as exc:
            raise self._error("prepare_client", "invalid_config", "qb.error.invalid_config") from exc
        return {
            "base_url": base_url,
            "username": username.strip(),
            "password": password,
            "remote_save_root": remote_save_root,
            "backend_import_root_path": backend_import_root_path,
        }

    def test_client(
        self,
        *,
        submitted_config: JsonObject,
        library: LibraryHandle,
    ) -> ProviderDiagnosticReport:
        try:
            provider = QbittorrentDownloadProvider(
                client=DownloadClientHandle(
                    client_id=0,
                    library=library,
                    provider_config=submitted_config,
                ),
                data_dir=self.data_dir,
                i18n=self.translations.i18n,
            )
            return provider.run_diagnostics()
        except ProviderOperationError as exc:
            return _diagnostic_report(
                [
                    ProviderDiagnosticCheck(
                        key="provider",
                        status="failed",
                        code=exc.code,
                        message=exc.safe_message,
                    )
                ]
            )
        except Exception:  # noqa: BLE001 - provider construction may fail in arbitrary ways
            return _diagnostic_report(
                [
                    ProviderDiagnosticCheck(
                        key="provider",
                        status="failed",
                        code="provider_test_failed",
                        message=self.translations.t("qb.error.client_test_failed"),
                    )
                ]
            )

    def build(self, *, client: DownloadClientHandle) -> QbittorrentDownloadProvider:
        return QbittorrentDownloadProvider(
            client=client,
            data_dir=self.data_dir,
            i18n=self.translations.i18n,
        )


class QbittorrentDownloadProvider(_TranslatedErrors):
    def __init__(
        self,
        *,
        client: DownloadClientHandle,
        data_dir: Path,
        i18n: object | None = None,
    ) -> None:
        self.translations = Translator(i18n)
        self.client_handle = client
        self.dead_hashes_path = Path(data_dir) / f"dead-qbittorrent-{client.client_id}.json"
        config = client.provider_config
        if not isinstance(config, dict):
            raise self._error("build_download", "invalid_config", "qb.error.invalid_config")
        try:
            self.base_url = _normalise_base_url(config.get("base_url"))
            username = config.get("username")
            password = config.get("password")
            if not isinstance(username, str) or not username.strip():
                raise ValueError("invalid username")
            if not isinstance(password, str) or not password:
                raise ValueError("invalid password")
            self.username = username.strip()
            self.password = password
            self.remote_save_root = _normalise_remote_root(config.get("remote_save_root"))
            self.backend_import_root_path = str(_root_path(config.get("backend_import_root_path")))
        except (OSError, TypeError, ValueError) as exc:
            raise self._error("build_download", "invalid_config", "qb.error.invalid_config") from exc
        try:
            import qbittorrentapi

            self.client = qbittorrentapi.Client(
                host=self.base_url,
                username=self.username,
                password=self.password,
                REQUESTS_ARGS={"timeout": 30},
                VERIFY_WEBUI_CERTIFICATE=True,
                FORCE_SCHEME_FROM_HOST=True,
            )
        except Exception as exc:
            raise self._error("build_download", "unavailable", "qb.error.client_unavailable", retryable=True) from exc
        self._logged_in = False

    def _dead_hashes(self) -> set[str]:
        try:
            raw = json.loads(self.dead_hashes_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, json.JSONDecodeError) as exc:
            raise self._error("dead_torrent_blacklist", "unavailable", "qb.error.dead_blacklist_unavailable", retryable=True) from exc
        if not isinstance(raw, list):
            raise self._error("dead_torrent_blacklist", "unavailable", "qb.error.dead_blacklist_unavailable", retryable=True)
        try:
            return {canonical_btih(value) for value in raw if isinstance(value, str)}
        except ValueError as exc:
            raise self._error("dead_torrent_blacklist", "unavailable", "qb.error.dead_blacklist_unavailable", retryable=True) from exc

    def _mark_dead(self, info_hash: str) -> None:
        hashes = self._dead_hashes()
        if info_hash in hashes:
            return
        hashes.add(info_hash)
        try:
            self.dead_hashes_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self.dead_hashes_path.with_name(
                f".{self.dead_hashes_path.name}.{secrets.token_hex(8)}.tmp"
            )
            temporary_path.write_text(
                json.dumps(sorted(hashes), ensure_ascii=False), encoding="utf-8"
            )
            os.replace(temporary_path, self.dead_hashes_path)
        except OSError as exc:
            raise self._error("dead_torrent_blacklist", "unavailable", "qb.error.dead_blacklist_unavailable", retryable=True) from exc

    @staticmethod
    def _has_no_progress_for_day(item: object) -> bool:
        raw_state = str(_value(item, "state", "")).strip()
        if raw_state not in {"downloading", "stalledDL", "metaDL", "forceDL", "forcedDL"}:
            return False
        dlspeed = _value(item, "dlspeed", None)
        last_activity = _value(item, "last_activity", None)
        if isinstance(dlspeed, bool) or isinstance(last_activity, bool):
            return False
        try:
            speed = int(dlspeed)
            activity_at = int(last_activity)
        except (TypeError, ValueError):
            return False
        now = int(time.time())
        return speed == 0 and 0 < activity_at <= now - DEAD_TORRENT_IDLE_SECONDS

    def _login(self) -> None:
        if self._logged_in:
            return
        try:
            self.client.auth_log_in()
        except Exception as exc:
            raise self._project_qb_error("qBittorrent_login", "qb.error.login_failed", exc) from exc
        self._logged_in = True

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        for name in ("http_status_code", "status_code"):
            value = getattr(exc, name, None)
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                pass
        return None

    @staticmethod
    def _exception_names(exc: Exception) -> set[str]:
        return {cls.__name__ for cls in type(exc).__mro__}

    @classmethod
    def _is_authentication_error(cls, exc: Exception) -> bool:
        return bool(
            cls._exception_names(exc) & {"LoginFailed", "Unauthorized401Error", "Forbidden403Error"}
        ) or cls._status_code(exc) in {401, 403}

    @classmethod
    def _is_retryable_error(cls, exc: Exception) -> bool:
        names = cls._exception_names(exc)
        return bool(
            names
            & {
                "APIConnectionError",
                "HTTP5XXError",
                "TimeoutError",
                "ConnectionError",
                "ConnectError",
                "ReadTimeout",
                "ConnectTimeout",
            }
        ) or (cls._status_code(exc) or 0) >= 500

    def _project_qb_error(
        self, operation: str, key: str, exc: Exception
    ) -> ProviderOperationError:
        if self._is_authentication_error(exc):
            return self._error(operation, "authentication_failed", "qb.error.authentication_failed")
        if self._is_retryable_error(exc):
            return self._error(operation, "unavailable", key, retryable=True)
        status = self._status_code(exc)
        if status is not None and 400 <= status < 500:
            return self._error(operation, "invalid_config", "qb.error.invalid_request")
        return self._error(operation, "unavailable", key, retryable=True)

    def run_diagnostics(self) -> ProviderDiagnosticReport:
        checks: list[ProviderDiagnosticCheck] = []
        try:
            self._login()
            version = self.client.app_version()
            web_api_version = self.client.app_web_api_version()
        except ProviderOperationError as exc:
            checks.append(
                ProviderDiagnosticCheck(
                    key="qbittorrent_connection",
                    status="failed",
                    code=exc.code,
                    message=exc.safe_message,
                )
            )
            checks.extend(
                (
                    ProviderDiagnosticCheck(
                        key="directory_mapping",
                        status="skipped",
                        code="connection_failed",
                        message=self.translations.t("qb.diagnostic.connection_failed_mapping"),
                    ),
                    ProviderDiagnosticCheck(
                        key="hardlink",
                        status="skipped",
                        code="connection_failed",
                        message=self.translations.t("qb.diagnostic.connection_failed_hardlink"),
                    ),
                )
            )
            return _diagnostic_report(checks)
        except Exception as exc:  # noqa: BLE001 - qB API errors are provider-specific
            projected = self._project_qb_error(
                "qBittorrent_connection",
                "qb.error.connection_test_failed",
                exc,
            )
            checks.append(
                ProviderDiagnosticCheck(
                    key="qbittorrent_connection",
                    status="failed",
                    code=projected.code,
                    message=projected.safe_message,
                )
            )
            checks.extend(
                (
                    ProviderDiagnosticCheck(
                        key="directory_mapping",
                        status="skipped",
                        code="connection_failed",
                        message=self.translations.t("qb.diagnostic.connection_failed_mapping"),
                    ),
                    ProviderDiagnosticCheck(
                        key="hardlink",
                        status="skipped",
                        code="connection_failed",
                        message=self.translations.t("qb.diagnostic.connection_failed_hardlink"),
                    ),
                )
            )
            return _diagnostic_report(checks)

        checks.append(
            ProviderDiagnosticCheck(
                key="qbittorrent_connection",
                status="ok",
                code="connected",
                message=self.translations.t("qb.diagnostic.connected"),
                details={
                    "version": str(version) if version is not None else None,
                    "web_api_version": str(web_api_version) if web_api_version is not None else None,
                },
            )
        )
        return self._run_storage_diagnostics(checks)

    def _run_storage_diagnostics(
        self,
        checks: list[ProviderDiagnosticCheck],
    ) -> ProviderDiagnosticReport:
        backend_root = Path(self.backend_import_root_path)
        try:
            media_root = _root_path(
                self.client_handle.library.provider_config.get("media_root_path")
            )
        except (TypeError, ValueError, OSError):
            checks.extend(
                (
                    ProviderDiagnosticCheck(
                        key="directory_mapping",
                        status="failed",
                        code="invalid_media_root",
                        message=self.translations.t("qb.diagnostic.invalid_media_root"),
                    ),
                    ProviderDiagnosticCheck(
                        key="hardlink",
                        status="skipped",
                        code="mapping_failed",
                        message=self.translations.t("qb.diagnostic.media_root_invalid_hardlink"),
                    ),
                )
            )
            return _diagnostic_report(checks)

        if not backend_root.exists() or not backend_root.is_dir():
            checks.extend(
                (
                    ProviderDiagnosticCheck(
                        key="directory_mapping",
                        status="failed",
                        code="backend_root_not_accessible",
                        message=self.translations.t("qb.diagnostic.backend_root_missing"),
                        details={"backend_root": str(backend_root)},
                    ),
                    ProviderDiagnosticCheck(
                        key="hardlink",
                        status="skipped",
                        code="mapping_failed",
                        message=self.translations.t("qb.diagnostic.backend_root_unavailable_hardlink"),
                    ),
                )
            )
            return _diagnostic_report(checks)

        probe_id = secrets.token_hex(12)
        backend_diagnostic_root = backend_root / ".sakuramedia-diagnostics"
        media_diagnostic_root = media_root / ".sakuramedia-diagnostics"
        backend_probe_dir = backend_diagnostic_root / probe_id
        media_probe_dir = media_diagnostic_root / probe_id
        sentinel_path = backend_probe_dir / "sentinel.txt"
        hardlink_path = media_probe_dir / "sentinel.link"
        remote_probe_dir = posixpath.join(
            self.remote_save_root,
            ".sakuramedia-diagnostics",
            probe_id,
        )
        backend_diagnostic_root_existed = backend_diagnostic_root.exists()
        media_diagnostic_root_existed = media_diagnostic_root.exists()
        cleanup_errors: list[str] = []

        try:
            _reject_symlink_components(backend_diagnostic_root)
            _reject_symlink_components(media_diagnostic_root)
            backend_probe_dir.mkdir(parents=True, exist_ok=False)
            sentinel_path.write_text("sakuramedia download diagnostic\n", encoding="utf-8")
            try:
                entries = self.client.app_get_directory_content(remote_probe_dir)
                names = set()
                for entry in entries:
                    name = _directory_entry_name(entry)
                    if name:
                        names.add(name)
            except Exception as exc:  # noqa: BLE001 - qB API errors are provider-specific
                projected = self._project_qb_error(
                    "directory_mapping",
                    "qb.error.directory_read_failed",
                    exc,
                )
                checks.append(
                    ProviderDiagnosticCheck(
                        key="directory_mapping",
                        status="failed",
                        code=projected.code,
                        message=projected.safe_message,
                        details={
                            "remote_path": remote_probe_dir,
                            "backend_path": str(backend_probe_dir),
                        },
                    )
                )
            else:
                if sentinel_path.name not in names:
                    checks.append(
                        ProviderDiagnosticCheck(
                            key="directory_mapping",
                            status="failed",
                            code="directory_mapping_mismatch",
                            message=self.translations.t("qb.diagnostic.mapping_mismatch"),
                            details={
                                "remote_path": remote_probe_dir,
                                "backend_path": str(backend_probe_dir),
                            },
                        )
                    )
                else:
                    checks.append(
                        ProviderDiagnosticCheck(
                            key="directory_mapping",
                            status="ok",
                            code="directory_mapping_matched",
                            message=self.translations.t("qb.diagnostic.mapping_ok"),
                            details={
                                "remote_path": remote_probe_dir,
                                "backend_path": str(backend_probe_dir),
                            },
                        )
                    )

            if checks[-1].key != "directory_mapping" or checks[-1].status != "ok":
                checks.append(
                    ProviderDiagnosticCheck(
                        key="hardlink",
                        status="skipped",
                        code="mapping_failed",
                        message=self.translations.t("qb.diagnostic.mapping_failed_hardlink"),
                    )
                )
            else:
                try:
                    media_probe_dir.mkdir(parents=True, exist_ok=True)
                    _reject_symlink_components(media_probe_dir)
                    if not media_probe_dir.is_dir():
                        raise OSError("diagnostic path is not a directory")
                    os.link(sentinel_path, hardlink_path)
                    source_stat = sentinel_path.stat()
                    target_stat = hardlink_path.stat()
                    if (source_stat.st_dev, source_stat.st_ino) != (
                        target_stat.st_dev,
                        target_stat.st_ino,
                    ):
                        raise OSError("hardlink identity mismatch")
                    checks.append(
                        ProviderDiagnosticCheck(
                            key="hardlink",
                            status="ok",
                            code="hardlink_supported",
                            message=self.translations.t("qb.diagnostic.hardlink_ok"),
                        )
                    )
                except (OSError, ValueError):
                    checks.append(
                        ProviderDiagnosticCheck(
                            key="hardlink",
                            status="warning",
                            code="hardlink_unavailable",
                            message=self.translations.t("qb.diagnostic.hardlink_unavailable"),
                        )
                    )
        except (OSError, ValueError):
            checks.append(
                ProviderDiagnosticCheck(
                    key="directory_mapping",
                    status="failed",
                    code="diagnostic_filesystem_error",
                    message=self.translations.t("qb.diagnostic.backend_write_failed"),
                )
            )
            checks.append(
                ProviderDiagnosticCheck(
                    key="hardlink",
                    status="skipped",
                    code="mapping_failed",
                    message=self.translations.t("qb.diagnostic.probe_create_failed"),
                )
            )
        finally:
            for path in (hardlink_path, sentinel_path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    cleanup_errors.append("file")
            for path in (media_probe_dir, backend_probe_dir):
                try:
                    path.rmdir()
                except OSError:
                    pass
            for path, existed in (
                (media_diagnostic_root, media_diagnostic_root_existed),
                (backend_diagnostic_root, backend_diagnostic_root_existed),
            ):
                if not existed and path.exists():
                    try:
                        path.rmdir()
                    except OSError:
                        cleanup_errors.append("directory")

        if cleanup_errors:
            checks.append(
                ProviderDiagnosticCheck(
                    key="cleanup",
                    status="warning",
                    code="diagnostic_cleanup_incomplete",
                    message=self.translations.t("qb.diagnostic.cleanup_incomplete"),
                )
            )
        return _diagnostic_report(checks)

    @classmethod
    def _is_conflict(cls, exc: Exception) -> bool:
        return "Conflict409Error" in cls._exception_names(exc) or cls._status_code(exc) == 409

    @classmethod
    def _is_missing(cls, exc: Exception) -> bool:
        return (
            bool(cls._exception_names(exc) & {"NotFound404Error", "TorrentNotFoundError"})
            or cls._status_code(exc) == 404
        )

    @staticmethod
    def _parse_torrent_hash(payload: bytes) -> str:
        try:
            import libtorrent as lt

            return canonical_btih(str(lt.torrent_info(payload).info_hash()))
        except Exception as exc:
            raise ValueError("invalid torrent file") from exc

    def _resolve_http_source(self, url: str) -> tuple[Literal["magnet", "torrent"], str | bytes]:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("invalid torrent url")
        try:
            with httpx.Client(timeout=120.0, follow_redirects=False, trust_env=False) as http_client:
                request_url = url
                for _ in range(MAX_HTTP_REDIRECTS):
                    with http_client.stream("GET", request_url) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise self._error("submit", "unsupported", "qb.error.redirect_location_invalid")
                            request_url = urljoin(request_url, location)
                            if _is_magnet(request_url):
                                return "magnet", request_url
                            parsed = urlsplit(request_url)
                            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                                raise self._error("submit", "unsupported", "qb.error.redirect_unsupported")
                            continue
                        status = int(response.status_code)
                        if status == 404:
                            raise self._error("submit", "source_not_found", "qb.error.torrent_not_found")
                        if 400 <= status < 500:
                            raise self._error("submit", "unsupported", "qb.error.torrent_url_unsupported")
                        if status >= 500:
                            raise self._error(
                                "submit", "unavailable", "qb.error.torrent_service_unavailable", retryable=True
                            )
                        if not 200 <= status < 300:
                            raise self._error("submit", "unsupported", "qb.error.torrent_url_unsupported")
                        content_length = response.headers.get("content-length")
                        if content_length is not None:
                            try:
                                if int(content_length) > MAX_TORRENT_BYTES:
                                    raise self._error("submit", "unsupported", "qb.error.torrent_too_large")
                            except ValueError:
                                raise self._error("submit", "unsupported", "qb.error.torrent_response_invalid") from None
                        chunks: list[bytes] = []
                        total = 0
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > MAX_TORRENT_BYTES:
                                raise self._error("submit", "unsupported", "qb.error.torrent_too_large")
                            chunks.append(chunk)
                        return "torrent", b"".join(chunks)
                raise self._error("submit", "unsupported", "qb.error.too_many_redirects")
        except ProviderOperationError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise self._error("submit", "unavailable", "qb.error.torrent_download_failed", retryable=True) from exc

    def _source(
        self, source_uri: str, torrent_bytes: bytes | None = None
    ) -> tuple[str, Literal["magnet", "torrent"], str | bytes]:
        if torrent_bytes is not None:
            if len(torrent_bytes) > MAX_TORRENT_BYTES:
                raise self._error("submit", "unsupported", "qb.error.torrent_too_large")
            return self._parse_torrent_hash(torrent_bytes), "torrent", torrent_bytes
        if _is_magnet(source_uri):
            try:
                info_hash = parse_hash_from_magnet(source_uri)
            except ValueError as exc:
                raise self._error("submit", "invalid_config", "qb.error.invalid_magnet") from exc
            return info_hash, "magnet", source_uri
        try:
            source_kind, payload = self._resolve_http_source(source_uri)
            if source_kind == "magnet":
                return parse_hash_from_magnet(payload), "magnet", payload
            return self._parse_torrent_hash(payload), "torrent", payload
        except ProviderOperationError:
            raise
        except ValueError as exc:
            raise self._error("submit", "invalid_config", "qb.error.invalid_torrent") from exc

    @staticmethod
    def _add_succeeded(response: object) -> bool:
        if isinstance(response, str):
            return response.strip().lower() == "ok."
        failure_count = _value(response, "failure_count", 0)
        try:
            return int(failure_count or 0) == 0
        except (TypeError, ValueError):
            return False

    def submit(self, *, submission: DownloadSubmission) -> RemoteDownloadTask:
        if not isinstance(submission, DownloadSubmission):
            raise self._error("submit", "invalid_config", "qb.error.invalid_submission")
        try:
            display_name = _safe_display_name(submission.display_name)
            source_uri = submission.source_uri.strip()
            if not source_uri:
                raise ValueError("empty source")
        except (AttributeError, TypeError, ValueError) as exc:
            raise self._error("submit", "invalid_config", "qb.error.invalid_submission") from exc
        info_hash, source_kind, source_payload = self._source(
            source_uri, getattr(submission, "torrent_bytes", None)
        )
        if info_hash in self._dead_hashes():
            logger.info(
                "qB torrent submission rejected because the hash is blacklisted "
                "client_id=%s info_hash=%s",
                self.client_handle.client_id,
                info_hash,
            )
            raise self._error("submit", "source_blacklisted", "qb.error.hash_blacklisted")
        tags = f"{SYSTEM_TAG},{CLIENT_TAG_PREFIX}{self.client_handle.client_id}"
        save_path = _remote_save_path(self.remote_save_root, display_name)
        self._login()
        added = False
        try:
            if source_kind == "magnet":
                response = self.client.torrents_add(
                    urls=source_payload,
                    tags=tags,
                    save_path=save_path,
                    rename=display_name,
                )
            else:
                response = self.client.torrents_add(
                    torrent_files=source_payload,
                    tags=tags,
                    save_path=save_path,
                    rename=display_name,
                )
            if not self._add_succeeded(response):
                raise RuntimeError("qBittorrent rejected torrent")
            added = True
        except Exception as exc:
            if self._is_conflict(exc):
                pass
            elif isinstance(exc, ProviderOperationError):
                raise
            else:
                raise self._project_qb_error("submit", "qb.error.submit_failed", exc) from exc
        item = None
        attempts = 3 if added else 1
        for attempt in range(attempts):
            item = self._find(info_hash, operation="submit")
            if item is not None or attempt + 1 == attempts:
                break
            time.sleep(0.1)
        if item is None:
            raise self._error("submit", "unavailable", "qb.error.submitted_task_missing", retryable=True)
        if not _managed(_value(item, "tags", ""), self.client_handle.client_id):
            raise self._error("submit", "task_not_managed", "qb.error.existing_task_unmanaged")
        task = self._task(item)
        if task is None:
            raise self._error("submit", "unavailable", "qb.error.task_invalid", retryable=True)
        return task

    def _completed_ref(self, item: object) -> JsonObject | None:
        content_path = _value(item, "content_path", _value(item, "save_path", ""))
        if not isinstance(content_path, str) or not content_path.strip():
            return None
        if "\x00" in content_path or "\\" in content_path:
            return None
        normalized = PurePosixPath(posixpath.normpath(content_path.strip()))
        root = PurePosixPath(self.remote_save_root)
        try:
            relative = normalized.relative_to(root).as_posix()
        except ValueError:
            return None
        if relative == "." or relative.startswith("../") or "/../" in relative:
            return None
        parts = tuple(relative.split("/")) if relative else ()
        if any(part in {"", ".", ".."} for part in parts):
            return None
        return {
            "version": LOCAL_REF_VERSION,
            "kind": LOCAL_REF_KIND,
            "root_path": self.backend_import_root_path,
            "relative_path": relative,
        }

    def _task(self, item: object) -> RemoteDownloadTask | None:
        try:
            info_hash = _torrent_hash(item)
        except ValueError:
            return None
        if not info_hash:
            return None
        state = map_qb_state(_value(item, "state", ""))
        completed_ref = self._completed_ref(item) if state == "completed" else None
        if state == "completed" and completed_ref is None:
            state = "failed"
        return RemoteDownloadTask(
            remote_id=info_hash,
            name=str(_value(item, "name", "") or ""),
            state=state,
            progress=_progress(_value(item, "progress", 0), completed=state == "completed"),
            completed_source_ref=completed_ref,
        )

    def list_tasks(self) -> tuple[RemoteDownloadTask, ...]:
        self._login()
        try:
            items = self.client.torrents_info(tag=SYSTEM_TAG)
            items = tuple(items)
        except Exception as exc:
            raise self._project_qb_error("list_tasks", "qb.error.task_read_failed", exc) from exc
        result: list[RemoteDownloadTask] = []
        for item in items:
            if not _managed(_value(item, "tags", ""), self.client_handle.client_id):
                continue
            task = self._task(item)
            if task is not None:
                reason = None
                if str(_value(item, "state", "")).strip() in {"error", "missingFiles"}:
                    reason = "qb_failed"
                elif self._has_no_progress_for_day(item):
                    reason = "no_progress_24h"
                if reason is not None:
                    try:
                        self._mark_dead(task.remote_id)
                        self.delete_task(remote_id=task.remote_id, delete_files=True)
                    except ProviderOperationError as exc:
                        logger.warning(
                            "qB dead torrent cleanup failed client_id=%s info_hash=%s "
                            "name=%s code=%s",
                            self.client_handle.client_id,
                            task.remote_id,
                            task.name,
                            exc.code,
                        )
                        result.append(task)
                    else:
                        logger.info(
                            "qB torrent blacklisted and removed client_id=%s info_hash=%s "
                            "name=%s reason=%s state=%s dlspeed=%s last_activity=%s",
                            self.client_handle.client_id,
                            task.remote_id,
                            task.name,
                            reason,
                            _value(item, "state", None),
                            _value(item, "dlspeed", None),
                            _value(item, "last_activity", None),
                        )
                    continue
                result.append(task)
        result.sort(key=lambda task: task.remote_id)
        return tuple(result)

    def _find(self, remote_id: str, *, operation: str) -> object | None:
        try:
            items = self.client.torrents_info(torrent_hashes=[remote_id])
        except Exception as exc:
            if self._is_missing(exc):
                return None
            raise self._project_qb_error(operation, "qb.error.task_read_failed", exc) from exc
        return next(iter(items), None)

    def delete_task(self, *, remote_id: str, delete_files: bool) -> None:
        if not isinstance(remote_id, str) or not remote_id.strip() or not isinstance(delete_files, bool):
            raise self._error("delete_task", "invalid_config", "qb.error.invalid_task_params")
        remote_id = remote_id.strip()
        self._login()
        item = self._find(remote_id, operation="delete_task")
        if item is not None and not _managed(_value(item, "tags", ""), self.client_handle.client_id):
            raise self._error("delete_task", "task_not_managed", "qb.error.task_not_managed")
        shell = (
            self._backend_path_for_save_path(_value(item, "save_path", ""))
            if item is not None
            else None
        )
        if item is not None:
            try:
                self.client.torrents_delete(
                    torrent_hashes=remote_id,
                    delete_files=delete_files,
                )
            except Exception as exc:
                if not self._is_missing(exc):
                    raise self._project_qb_error("delete_task", "qb.error.delete_task_failed", exc) from exc
        if delete_files:
            self._remove_task_directories(remote_id, preferred=shell)

    def _backend_path_for_save_path(self, value: object) -> Path | None:
        """将 qB 侧 save_path 映射为后端可见的任务目录。

        只接受下载根下的单段子目录；其余（根外、嵌套、符号链接）一律视为不归
        本客户端所有，不做删除。
        """
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip()
        if "\x00" in text or "\\" in text:
            return None
        root = PurePosixPath(self.remote_save_root)
        normalized = PurePosixPath(posixpath.normpath(text))
        try:
            relative = normalized.relative_to(root).as_posix()
        except ValueError:
            return None
        if not relative or relative == "." or "/" in relative or ".." in relative.split("/"):
            return None
        candidate = Path(self.backend_import_root_path) / relative
        try:
            _reject_symlink_components(candidate)
        except ValueError:
            return None
        return candidate

    def _remove_task_directories(self, remote_id: str, *, preferred: Path | None) -> None:
        """尽力删除任务目录：优先 qB 报告的 save_path，缺失时按命名约定兜底。

        兜底只认目录名等于 info_hash 或以 `-<hash 前 6 位>` 结尾、且位于下载根下
        的子目录；匹配到多个（理论上极端哈希前缀碰撞）时放弃，避免误删。
        """
        if preferred is not None:
            candidates = [preferred]
        else:
            candidates = self._leftover_task_directories(remote_id)
            if len(candidates) > 1:
                logger.warning(
                    "qB task directory cleanup skipped: ambiguous matches client_id=%s info_hash=%s paths=%s",
                    self.client_handle.client_id,
                    remote_id,
                    [str(path) for path in candidates],
                )
                return
        for path in candidates:
            try:
                if not path.is_dir():
                    continue
                _reject_symlink_components(path)
                shutil.rmtree(path)
                logger.info(
                    "qB task directory removed client_id=%s info_hash=%s path=%s",
                    self.client_handle.client_id,
                    remote_id,
                    path,
                )
            except (OSError, ValueError) as exc:
                logger.warning(
                    "qB task directory cleanup failed client_id=%s info_hash=%s path=%s error=%s",
                    self.client_handle.client_id,
                    remote_id,
                    path,
                    exc,
                )

    def _leftover_task_directories(self, remote_id: str) -> list[Path]:
        root = Path(self.backend_import_root_path)
        suffix = f"-{remote_id[:6].lower()}"
        try:
            children = list(root.iterdir())
        except OSError as exc:
            logger.warning("qB leftover directory scan failed root=%s error=%s", root, exc)
            return []
        matches: list[Path] = []
        for child in children:
            if not child.is_dir():
                continue
            name = child.name
            if name == remote_id or name.lower().endswith(suffix):
                matches.append(child)
        return matches
