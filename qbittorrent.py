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


def _error(
    operation: str,
    code: Literal[
        "invalid_config",
        "authentication_failed",
        "source_not_found",
        "task_not_managed",
        "source_blacklisted",
        "unsupported",
        "unavailable",
    ],
    message: str,
    *,
    retryable: bool = False,
) -> ProviderOperationError:
    return ProviderOperationError(
        provider_key="local",
        operation=operation,
        code=code,
        safe_message=message,
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


class QbittorrentDownloadComponent:
    """Declaration and construction boundary for qBittorrent downloads."""

    def __init__(self, *, data_dir: Path) -> None:
        self.data_dir = data_dir

    config_fields = (
        ConfigField(
            key="base_url",
            label="qBittorrent 地址",
            input="text",
            required=True,
            description="qBittorrent WebUI 地址，后端会使用它进行连接测试和提交下载任务。",
            hint="例如 https://qb.example.com",
        ),
        ConfigField(
            key="username",
            label="用户名",
            input="text",
            required=True,
            description="qBittorrent WebUI 登录用户名。",
        ),
        ConfigField(
            key="password",
            label="密码",
            input="secret",
            required=True,
            description="qBittorrent WebUI 登录密码；编辑时留空表示保留已保存密码。",
        ),
        ConfigField(
            key="remote_save_root",
            label="qBittorrent 保存根目录",
            input="path",
            required=True,
            description="qBittorrent 进程看到的下载根目录，例如 /downloads。",
            hint="例如 /downloads",
        ),
        ConfigField(
            key="backend_import_root_path",
            label="后端下载根目录",
            input="path",
            required=True,
            description="后端进程看到的同一下载根目录；必须与 qB 路径映射到同一个宿主机目录。",
            hint="例如 /mnt/qb-downloads",
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
            raise _error("prepare_client", "invalid_config", "qBittorrent 配置字段无效")
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
            raise _error("prepare_client", "invalid_config", "qBittorrent 配置无效") from exc
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
                        message="下载器测试失败",
                    )
                ]
            )

    def build(self, *, client: DownloadClientHandle) -> QbittorrentDownloadProvider:
        return QbittorrentDownloadProvider(client=client, data_dir=self.data_dir)


class QbittorrentDownloadProvider:
    def __init__(self, *, client: DownloadClientHandle, data_dir: Path) -> None:
        self.client_handle = client
        self.dead_hashes_path = Path(data_dir) / f"dead-qbittorrent-{client.client_id}.json"
        config = client.provider_config
        if not isinstance(config, dict):
            raise _error("build_download", "invalid_config", "qBittorrent 配置无效")
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
            raise _error("build_download", "invalid_config", "qBittorrent 配置无效") from exc
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
            raise _error("build_download", "unavailable", "qBittorrent 客户端不可用", retryable=True) from exc
        self._logged_in = False

    def _dead_hashes(self) -> set[str]:
        try:
            raw = json.loads(self.dead_hashes_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, json.JSONDecodeError) as exc:
            raise _error("dead_torrent_blacklist", "unavailable", "死种黑名单不可用", retryable=True) from exc
        if not isinstance(raw, list):
            raise _error("dead_torrent_blacklist", "unavailable", "死种黑名单不可用", retryable=True)
        try:
            return {canonical_btih(value) for value in raw if isinstance(value, str)}
        except ValueError as exc:
            raise _error("dead_torrent_blacklist", "unavailable", "死种黑名单不可用", retryable=True) from exc

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
            raise _error("dead_torrent_blacklist", "unavailable", "死种黑名单不可用", retryable=True) from exc

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
            raise self._project_qb_error("qBittorrent_login", "qBittorrent 登录失败", exc) from exc
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

    @classmethod
    def _project_qb_error(
        cls, operation: str, message: str, exc: Exception
    ) -> ProviderOperationError:
        if cls._is_authentication_error(exc):
            return _error(operation, "authentication_failed", "qBittorrent 认证失败")
        if cls._is_retryable_error(exc):
            return _error(operation, "unavailable", message, retryable=True)
        status = cls._status_code(exc)
        if status is not None and 400 <= status < 500:
            return _error(operation, "invalid_config", "qBittorrent 请求参数无效")
        return _error(operation, "unavailable", message, retryable=True)

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
                        message="qBittorrent 连接失败，未执行目录映射测试。",
                    ),
                    ProviderDiagnosticCheck(
                        key="hardlink",
                        status="skipped",
                        code="connection_failed",
                        message="qBittorrent 连接失败，未执行硬链接测试。",
                    ),
                )
            )
            return _diagnostic_report(checks)
        except Exception as exc:  # noqa: BLE001 - qB API errors are provider-specific
            projected = self._project_qb_error(
                "qBittorrent_connection",
                "qBittorrent 连接测试失败",
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
                        message="qBittorrent 连接失败，未执行目录映射测试。",
                    ),
                    ProviderDiagnosticCheck(
                        key="hardlink",
                        status="skipped",
                        code="connection_failed",
                        message="qBittorrent 连接失败，未执行硬链接测试。",
                    ),
                )
            )
            return _diagnostic_report(checks)

        checks.append(
            ProviderDiagnosticCheck(
                key="qbittorrent_connection",
                status="ok",
                code="connected",
                message="qBittorrent 连接和认证成功。",
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
                        message="媒体库根目录配置无效。",
                    ),
                    ProviderDiagnosticCheck(
                        key="hardlink",
                        status="skipped",
                        code="mapping_failed",
                        message="媒体库根目录无效，未执行硬链接测试。",
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
                        message="后端下载根目录不存在或不是目录。",
                        details={"backend_root": str(backend_root)},
                    ),
                    ProviderDiagnosticCheck(
                        key="hardlink",
                        status="skipped",
                        code="mapping_failed",
                        message="后端下载根目录不可用，未执行硬链接测试。",
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
                    "qBittorrent 目录读取失败",
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
                            message="qBittorrent 看不到后端下载目录中的测试文件，两个路径未映射到同一目录。",
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
                            message="qBittorrent 下载目录与后端下载根目录映射正确。",
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
                        message="目录映射未通过，未执行硬链接测试。",
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
                            message="后端下载目录与媒体库支持硬链接。",
                        )
                    )
                except (OSError, ValueError):
                    checks.append(
                        ProviderDiagnosticCheck(
                            key="hardlink",
                            status="warning",
                            code="hardlink_unavailable",
                            message="下载目录与媒体库不支持硬链接，导入时会回退为复制。",
                        )
                    )
        except (OSError, ValueError):
            checks.append(
                ProviderDiagnosticCheck(
                    key="directory_mapping",
                    status="failed",
                    code="diagnostic_filesystem_error",
                    message="后端下载目录无法创建测试文件。",
                )
            )
            checks.append(
                ProviderDiagnosticCheck(
                    key="hardlink",
                    status="skipped",
                    code="mapping_failed",
                    message="测试文件创建失败，未执行硬链接测试。",
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
                    message="测试完成，但诊断临时文件未能全部清理。",
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

    @staticmethod
    def _resolve_http_source(url: str) -> tuple[Literal["magnet", "torrent"], str | bytes]:
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
                                raise _error("submit", "unsupported", "种子文件重定向地址无效")
                            request_url = urljoin(request_url, location)
                            if _is_magnet(request_url):
                                return "magnet", request_url
                            parsed = urlsplit(request_url)
                            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                                raise _error("submit", "unsupported", "种子文件重定向地址不受支持")
                            continue
                        status = int(response.status_code)
                        if status == 404:
                            raise _error("submit", "source_not_found", "种子文件不存在")
                        if 400 <= status < 500:
                            raise _error("submit", "unsupported", "种子文件地址不受支持")
                        if status >= 500:
                            raise _error(
                                "submit", "unavailable", "种子文件服务暂时不可用", retryable=True
                            )
                        if not 200 <= status < 300:
                            raise _error("submit", "unsupported", "种子文件地址不受支持")
                        content_length = response.headers.get("content-length")
                        if content_length is not None:
                            try:
                                if int(content_length) > MAX_TORRENT_BYTES:
                                    raise _error("submit", "unsupported", "种子文件超过大小限制")
                            except ValueError:
                                raise _error("submit", "unsupported", "种子文件响应无效") from None
                        chunks: list[bytes] = []
                        total = 0
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > MAX_TORRENT_BYTES:
                                raise _error("submit", "unsupported", "种子文件超过大小限制")
                            chunks.append(chunk)
                        return "torrent", b"".join(chunks)
                raise _error("submit", "unsupported", "种子文件重定向次数过多")
        except ProviderOperationError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise _error("submit", "unavailable", "种子文件下载失败", retryable=True) from exc

    def _source(self, source_uri: str) -> tuple[str, Literal["magnet", "torrent"], str | bytes]:
        if _is_magnet(source_uri):
            try:
                info_hash = parse_hash_from_magnet(source_uri)
            except ValueError as exc:
                raise _error("submit", "invalid_config", "磁力链接无效") from exc
            return info_hash, "magnet", source_uri
        try:
            source_kind, payload = self._resolve_http_source(source_uri)
            if source_kind == "magnet":
                return parse_hash_from_magnet(payload), "magnet", payload
            return self._parse_torrent_hash(payload), "torrent", payload
        except ProviderOperationError:
            raise
        except ValueError as exc:
            raise _error("submit", "invalid_config", "种子文件无效") from exc

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
            raise _error("submit", "invalid_config", "下载提交参数无效")
        try:
            display_name = _safe_display_name(submission.display_name)
            source_uri = submission.source_uri.strip()
            if not source_uri:
                raise ValueError("empty source")
        except (AttributeError, TypeError, ValueError) as exc:
            raise _error("submit", "invalid_config", "下载提交参数无效") from exc
        info_hash, source_kind, source_payload = self._source(source_uri)
        if info_hash in self._dead_hashes():
            logger.info(
                "qB torrent submission rejected because the hash is blacklisted "
                "client_id=%s info_hash=%s",
                self.client_handle.client_id,
                info_hash,
            )
            raise _error("submit", "source_blacklisted", "该种子已被列入下载黑名单")
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
                raise self._project_qb_error("submit", "qBittorrent 提交失败", exc) from exc
        item = None
        attempts = 3 if added else 1
        for attempt in range(attempts):
            item = self._find(info_hash, operation="submit")
            if item is not None or attempt + 1 == attempts:
                break
            time.sleep(0.1)
        if item is None:
            raise _error("submit", "unavailable", "qBittorrent 未返回已提交任务", retryable=True)
        if not _managed(_value(item, "tags", ""), self.client_handle.client_id):
            raise _error("submit", "task_not_managed", "同哈希下载任务已存在且不受当前客户端管理")
        task = self._task(item)
        if task is None:
            raise _error("submit", "unavailable", "qBittorrent 任务信息无效", retryable=True)
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
            raise self._project_qb_error("list_tasks", "qBittorrent 任务读取失败", exc) from exc
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
            raise self._project_qb_error(operation, "qBittorrent 任务读取失败", exc) from exc
        return next(iter(items), None)

    def delete_task(self, *, remote_id: str, delete_files: bool) -> None:
        if not isinstance(remote_id, str) or not remote_id.strip() or not isinstance(delete_files, bool):
            raise _error("delete_task", "invalid_config", "下载任务参数无效")
        self._login()
        item = self._find(remote_id.strip(), operation="delete_task")
        if item is None:
            return
        if not _managed(_value(item, "tags", ""), self.client_handle.client_id):
            raise _error("delete_task", "task_not_managed", "下载任务不受当前客户端管理")
        try:
            self.client.torrents_delete(
                torrent_hashes=remote_id.strip(),
                delete_files=delete_files,
            )
        except Exception as exc:
            if self._is_missing(exc):
                return
            raise self._project_qb_error("delete_task", "qBittorrent 删除任务失败", exc) from exc
