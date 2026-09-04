"""Local filesystem implementation of the SakuraMedia storage protocol."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Literal

from starlette.responses import Response, StreamingResponse

from src.plugins.provider_protocol import (
    BrowseEntry,
    BrowsePage,
    ClipArtifact,
    ImportFile,
    ImportFileContent,
    ImportPlacement,
    JsonObject,
    LibraryHandle,
    MediaHandle,
    MediaTransferSource,
    MediaTransferSourceInfo,
    PlaybackContext,
    ProviderOperationError,
    StagedMedia,
    ThumbnailArtifact,
    ThumbnailGeneration,
)
from src.service.playback.media_metadata_probe_service import MediaMetadataProbeService

from .merged_playback import Mp4MergeError, build_layout, merged_range_requests_response

logger = logging.getLogger(__name__)

LOCAL_REF_VERSION = 1
MEDIA_REF_KIND = "media_local_path"
MANUAL_SOURCE_REF_KIND = "manual_local_path"
DOWNLOAD_SOURCE_REF_KIND = "download_local_path"
_HASH_MIB = 1024 * 1024
_HASH_HEAD_TAIL_BYTES = 3 * _HASH_MIB
_HASH_MIDDLE_BYTES = _HASH_MIB
_HASH_FULL_THRESHOLD = 8 * _HASH_MIB
_HASH_DOMAIN = b"media-file-hash-v1"
_OPERATION_KIND = "local_import"
_OPERATION_STATE_PREPARING = "preparing"
_OPERATION_STATE_STAGED = "staged"
_OPERATION_STATE_FINALIZED = "finalized"
_OPERATION_STATE_ABORTED = "aborted"
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_VIDEO_SUFFIXES = frozenset(
    {
        ".avi",
        ".3gp",
        ".flv",
        ".f4v",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mts",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".ogv",
        ".rm",
        ".rmvb",
        ".ts",
        ".webm",
        ".wmv",
    }
)


def _provider_error(
    operation: str,
    code: Literal[
        "invalid_config",
        "authentication_failed",
        "source_not_found",
        "unsupported",
        "unavailable",
    ],
    safe_message: str,
    *,
    retryable: bool = False,
) -> ProviderOperationError:
    # Keep user supplied paths and subprocess/decoder details out of the
    # message.  They may contain absolute paths or other sensitive details.
    return ProviderOperationError(
        provider_key="local",
        operation=operation,
        code=code,
        safe_message=safe_message,
        retryable=retryable,
    )


def _staged_media(
    *,
    storage_ref: JsonObject,
    receipt: JsonObject,
    size_bytes: int,
    duration_seconds: int | None,
    video_info: JsonObject | None,
    resolution: str | None,
) -> StagedMedia:
    """Build a staged result for both pre- and post-resolution v4 hosts."""
    if "resolution" in getattr(StagedMedia, "__dataclass_fields__", {}):
        return StagedMedia(
            storage_ref=storage_ref,
            receipt=receipt,
            size_bytes=size_bytes,
            duration_seconds=duration_seconds,
            video_info=video_info,
            resolution=resolution,
        )
    return StagedMedia(
        storage_ref=storage_ref,
        receipt=receipt,
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
        video_info=video_info,
    )


@dataclass(frozen=True)
class _TransferFileSnapshot:
    dev: int
    ino: int
    size: int
    mtime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _TransferFileSnapshot:
        return cls(
            dev=int(value.st_dev),
            ino=int(value.st_ino),
            size=int(value.st_size),
            mtime_ns=int(value.st_mtime_ns),
        )


class _LocalTransferReader:
    """Independent logical cursor over a source-owned stable file descriptor."""

    def __init__(self, source: _LocalTransferSource) -> None:
        self._source = source
        self._position = 0
        self._closed = False

    def _close(self) -> None:
        self._closed = True

    def read(self, size: int = -1) -> bytes:
        self._ensure_open()
        if isinstance(size, bool) or not isinstance(size, int):
            raise TypeError("read size must be an integer")
        remaining = self._source.info.size_bytes - self._position
        if size < 0:
            size = max(0, remaining)
        else:
            size = min(size, max(0, remaining))
        if size == 0:
            return b""
        try:
            data = os.pread(self._source._fd, size, self._position)
        except OSError as exc:
            raise _provider_error(
                "open_transfer_source", "unavailable", "媒体文件读取失败", retryable=True
            ) from exc
        self._position += len(data)
        return data

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self._ensure_open()
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise TypeError("seek offset must be an integer")
        if whence == os.SEEK_SET:
            position = offset
        elif whence == os.SEEK_CUR:
            position = self._position + offset
        elif whence == os.SEEK_END:
            position = self._source.info.size_bytes + offset
        else:
            raise ValueError("invalid seek whence")
        if position < 0:
            raise ValueError("negative seek position")
        self._position = position
        return position

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValueError("I/O operation on closed transfer reader")
        self._source._ensure_open()


class _LocalTransferSource:
    """Provider-private stable descriptor and the path snapshot it represents."""

    def __init__(self, *, owner: object, media: MediaHandle, path: Path, fd: int, snapshot: _TransferFileSnapshot) -> None:
        self._owner = owner
        self._media = media
        self._path = path
        self._fd = fd
        self._snapshot = snapshot
        self.info = MediaTransferSourceInfo(file_name=path.name, size_bytes=snapshot.size)
        self._closed = False

    @contextmanager
    def open_reader(self):
        self._ensure_open()
        reader = _LocalTransferReader(self)
        try:
            yield reader
        finally:
            reader._close()

    def assert_unchanged(self) -> None:
        self._ensure_open()
        try:
            descriptor = os.fstat(self._fd)
        except OSError as exc:
            raise _provider_error(
                "open_transfer_source", "unavailable", "媒体文件读取失败", retryable=True
            ) from exc
        if not stat.S_ISREG(descriptor.st_mode) or (
            _TransferFileSnapshot.from_stat(descriptor) != self._snapshot
        ):
            raise _provider_error("open_transfer_source", "source_not_found", "媒体文件已变化")
        try:
            current = os.stat(self._path, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise _provider_error("open_transfer_source", "source_not_found", "媒体文件已变化") from exc
        except OSError as exc:
            raise _provider_error(
                "open_transfer_source", "unavailable", "媒体文件读取失败", retryable=True
            ) from exc
        if not stat.S_ISREG(current.st_mode) or (
            _TransferFileSnapshot.from_stat(current) != self._snapshot
        ):
            raise _provider_error("open_transfer_source", "source_not_found", "媒体文件已变化")

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._fd)
        except OSError:
            pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValueError("I/O operation on closed transfer source")


def _is_video(name: str) -> bool:
    return Path(name).suffix.lower() in _VIDEO_SUFFIXES


def _relative_parts(value: object, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise ValueError("unsafe relative path")
    if not value and allow_empty:
        return ()
    if not value or value.startswith("/"):
        raise ValueError("unsafe relative path")
    parts = tuple(value.split("/"))
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("unsafe relative path")
    return parts


def _posix_relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _reject_symlink_components(path: Path, *, include_leaf: bool = True) -> None:
    """Reject a symlink anywhere in a path without following it."""
    current = Path(path.anchor) if path.anchor else Path()
    for index, part in enumerate(path.parts):
        if index == 0 and path.anchor and part == path.anchor:
            continue
        current = current / part
        if current.is_symlink() and (include_leaf or current != path):
            raise ValueError("symlink path")


def _root_path(value: object) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("invalid root")
    raw = Path(value).expanduser()
    # Relative configuration is made absolute once, so all refs remain
    # independent from a later process working-directory change.
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    _reject_symlink_components(raw)
    return raw.resolve(strict=False)


def _ensure_under(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes root") from exc


class LocalStorageProvider:
    """A provider-neutral local filesystem storage implementation."""

    def __init__(self, *, library: LibraryHandle, data_dir: Path):
        config = library.provider_config
        if not isinstance(config, dict):
            raise _provider_error("build_storage", "invalid_config", "本地存储配置无效")
        try:
            self.media_root = _root_path(config.get("media_root_path"))
            self.manual_import_root = _root_path(config.get("manual_import_root_path"))
            self.data_dir = _root_path(str(data_dir))
            self.operation_dir = self.data_dir / "operations"
            _reject_symlink_components(self.operation_dir, include_leaf=False)
        except (ValueError, OSError) as exc:
            raise _provider_error("build_storage", "invalid_config", "本地存储路径配置无效") from exc
        try:
            self.media_root.mkdir(parents=True, exist_ok=True)
            self.manual_import_root.mkdir(parents=True, exist_ok=True)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.operation_dir.mkdir(parents=True, exist_ok=True)
            _reject_symlink_components(self.media_root)
            _reject_symlink_components(self.manual_import_root)
            _reject_symlink_components(self.data_dir)
            _reject_symlink_components(self.operation_dir)
        except (OSError, ValueError) as exc:
            raise _provider_error(
                "build_storage", "unavailable", "本地存储目录不可用", retryable=True
            ) from exc

    @staticmethod
    def _media_ref(relative_path: str) -> JsonObject:
        return {
            "version": LOCAL_REF_VERSION,
            "kind": MEDIA_REF_KIND,
            "relative_path": relative_path,
        }

    def scan_managed_media_ref_keys(self) -> set[str]:
        """Enumerate regular files below the configured media root for validity checks."""
        try:
            _reject_symlink_components(self.media_root)
            media_root = self.media_root.resolve(strict=False)
            candidates = list(self.media_root.rglob("*"))
        except (OSError, ValueError) as exc:
            raise _provider_error(
                "scan_managed_media_ref_keys",
                "unavailable",
                "本地媒体目录读取失败",
                retryable=True,
            ) from exc
        relative_paths: set[str] = set()
        for path in candidates:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                _reject_symlink_components(path)
                _ensure_under(path.resolve(strict=True), media_root)
                relative_path = _posix_relative(path, self.media_root)
            except ValueError:
                continue
            except OSError as exc:
                raise _provider_error(
                    "scan_managed_media_ref_keys",
                    "unavailable",
                    "本地媒体文件读取失败",
                    retryable=True,
                ) from exc
            relative_paths.add(relative_path)
        return relative_paths

    @staticmethod
    def managed_media_ref_key(*, media_ref: JsonObject) -> str:
        if (
            media_ref.get("version") != LOCAL_REF_VERSION
            or media_ref.get("kind") != MEDIA_REF_KIND
        ):
            raise _provider_error(
                "managed_media_ref_key",
                "source_not_found",
                "本地媒体引用无效",
            )
        try:
            return "/".join(_relative_parts(media_ref.get("relative_path")))
        except ValueError as exc:
            raise _provider_error(
                "managed_media_ref_key",
                "source_not_found",
                "本地媒体引用无效",
            ) from exc

    @staticmethod
    def _manual_source_ref(relative_path: str) -> JsonObject:
        return {
            "version": LOCAL_REF_VERSION,
            "kind": MANUAL_SOURCE_REF_KIND,
            "relative_path": relative_path,
        }

    def _path_from_ref(
        self,
        ref: object,
        *,
        root: Path,
        operation: str,
        require_file: bool | None = None,
        expected_kind: str = MEDIA_REF_KIND,
    ) -> tuple[Path, str]:
        if not isinstance(ref, dict):
            raise _provider_error(operation, "source_not_found", "本地引用无效")
        if ref.get("version") != LOCAL_REF_VERSION or ref.get("kind") != expected_kind:
            raise _provider_error(operation, "source_not_found", "本地引用无效")
        try:
            parts = _relative_parts(ref.get("relative_path"), allow_empty=True)
            path = root.joinpath(*parts)
            _reject_symlink_components(path)
            resolved = path.resolve(strict=False)
            _ensure_under(resolved, root.resolve(strict=False))
        except (ValueError, OSError) as exc:
            raise _provider_error(operation, "source_not_found", "本地引用不可访问") from exc
        if require_file is True and (not path.is_file() or path.is_symlink()):
            raise _provider_error(operation, "source_not_found", "媒体文件不存在")
        if require_file is False and (not path.is_dir() or path.is_symlink()):
            raise _provider_error(operation, "source_not_found", "目录不存在")
        return path, "/".join(parts)

    def _source_root_for_ref(
        self,
        source_ref: object,
        *,
        operation: str,
    ) -> tuple[Path, str]:
        if not isinstance(source_ref, dict):
            raise _provider_error(operation, "source_not_found", "本地导入引用无效")
        if source_ref.get("version") != LOCAL_REF_VERSION:
            raise _provider_error(operation, "source_not_found", "本地导入引用无效")
        kind = source_ref.get("kind")
        if kind == MANUAL_SOURCE_REF_KIND:
            return self.manual_import_root, MANUAL_SOURCE_REF_KIND
        if kind == DOWNLOAD_SOURCE_REF_KIND:
            try:
                return _root_path(source_ref.get("root_path")), DOWNLOAD_SOURCE_REF_KIND
            except (OSError, ValueError) as exc:
                raise _provider_error(operation, "source_not_found", "下载导入根目录无效") from exc
        raise _provider_error(operation, "source_not_found", "本地导入引用无效")

    @staticmethod
    def _source_ref_with_relative(source_ref: JsonObject, relative_path: str) -> JsonObject:
        result = {
            "version": source_ref["version"],
            "kind": source_ref["kind"],
            "relative_path": relative_path,
        }
        if source_ref["kind"] == DOWNLOAD_SOURCE_REF_KIND:
            result["root_path"] = source_ref["root_path"]
        return result

    def _entry(self, path: Path, *, relative_path: str) -> BrowseEntry:
        try:
            stat = path.stat()
        except OSError as exc:
            raise _provider_error("browse", "unavailable", "本地目录读取失败", retryable=True) from exc
        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        return BrowseEntry(
            source_ref=self._manual_source_ref(relative_path),
            name=path.name,
            entry_type="directory" if path.is_dir() else "file",
            size_bytes=None if path.is_dir() else stat.st_size,
            modified_at=modified_at,
            is_video=path.is_file() and _is_video(path.name),
        )

    def browse(
        self, *, parent_ref: JsonObject | None, cursor: str | None, limit: int
    ) -> BrowsePage:
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise _provider_error("browse", "invalid_config", "浏览分页参数无效")
        parent_path, parent_relative = (
            (self.manual_import_root, "")
            if parent_ref is None
            else self._path_from_ref(
                parent_ref,
                root=self.manual_import_root,
                operation="browse",
                require_file=False,
                expected_kind=MANUAL_SOURCE_REF_KIND,
            )
        )
        try:
            offset = 0 if cursor in (None, "") else int(cursor)
            if offset < 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise _provider_error("browse", "invalid_config", "浏览游标无效") from exc
        try:
            children = []
            for child in parent_path.iterdir():
                # Symlink entries are never exposed or followed.
                if child.is_symlink() or not (child.is_dir() or child.is_file()):
                    continue
                relative = "/".join(filter(None, (parent_relative, child.name)))
                children.append((child.name.casefold(), child.name, self._entry(child, relative_path=relative)))
        except OSError as exc:
            raise _provider_error("browse", "unavailable", "本地目录读取失败", retryable=True) from exc
        children.sort(key=lambda item: (item[0], item[1]))
        page = children[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(children) else None
        return BrowsePage(entries=tuple(item[2] for item in page), next_cursor=next_cursor)

    def scan_import_source(self, *, source_ref: JsonObject) -> tuple[ImportFile, ...]:
        source_root, source_kind = self._source_root_for_ref(
            source_ref,
            operation="scan_import_source",
        )
        source_path, _source_relative = self._path_from_ref(
            source_ref,
            root=source_root,
            operation="scan_import_source",
            expected_kind=source_kind,
        )
        if not source_path.exists() or not (source_path.is_file() or source_path.is_dir()):
            raise _provider_error("scan_import_source", "source_not_found", "扫描目录不存在")
        try:
            candidates = [source_path] if source_path.is_file() else list(source_path.rglob("*"))
        except OSError as exc:
            raise _provider_error(
                "scan_import_source", "unavailable", "本地目录扫描失败", retryable=True
            ) from exc
        result: list[ImportFile] = []
        for candidate in candidates:
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                _reject_symlink_components(candidate)
                resolved = candidate.resolve(strict=True)
                _ensure_under(resolved, source_root.resolve(strict=False))
                relative = _posix_relative(candidate, source_root)
                stat = candidate.stat()
            except (OSError, ValueError) as exc:
                if isinstance(exc, ValueError):
                    continue
                raise _provider_error(
                    "scan_import_source", "unavailable", "本地文件读取失败", retryable=True
                ) from exc
            result.append(
                ImportFile(
                    source_ref=self._source_ref_with_relative(source_ref, relative),
                    name=candidate.name,
                    relative_path=relative,
                    size_bytes=stat.st_size,
                    is_video=_is_video(candidate.name),
                )
            )
        result.sort(key=lambda item: (item.relative_path.casefold(), item.relative_path))
        return tuple(result)

    def get_import_source_identity(self, *, source: ImportFile) -> str:
        path, _relative, identity = self._source_for(source)
        payload = json.dumps(
            {"path": str(path), **identity},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"local-import-source-v1:{hashlib.sha256(payload).hexdigest()}"

    def read_import_file(self, *, source: ImportFile) -> ImportFileContent:
        path, relative, identity = self._source_for(source)
        try:
            content = path.read_bytes()
            if len(content) != identity["source_size"]:
                raise OSError("source changed during read")
        except OSError as exc:
            raise _provider_error(
                "read_import_file", "unavailable", "导入源读取失败", retryable=True
            ) from exc
        return ImportFileContent(
            content=content,
            deletion_receipt={
                "version": LOCAL_REF_VERSION,
                "kind": "local_import_file",
                "source_ref": source.source_ref,
                "source_relative_path": relative,
                **identity,
            },
        )

    def _reject_media_library_source(self, path: Path, *, operation: str) -> None:
        try:
            path.resolve(strict=False).relative_to(self.media_root.resolve(strict=False))
        except ValueError:
            return
        except OSError as exc:
            raise _provider_error(
                operation, "unavailable", "导入源路径不可用", retryable=True
            ) from exc
        raise _provider_error(
            operation,
            "unsupported",
            "不允许删除媒体库内的导入源",
        )

    def delete_import_file(self, *, receipt: JsonObject) -> None:
        if not isinstance(receipt, dict):
            raise _provider_error("delete_import_file", "source_not_found", "导入源引用无效")
        if receipt.get("version") != LOCAL_REF_VERSION or receipt.get("kind") != "local_import_file":
            raise _provider_error("delete_import_file", "source_not_found", "导入源引用无效")
        source_ref = receipt.get("source_ref")
        source_root, source_kind = self._source_root_for_ref(
            source_ref,
            operation="delete_import_file",
        )
        source, relative = self._path_from_ref(
            source_ref,
            root=source_root,
            operation="delete_import_file",
            require_file=True,
            expected_kind=source_kind,
        )
        if receipt.get("source_relative_path") != relative:
            raise _provider_error("delete_import_file", "source_not_found", "导入源引用无效")
        identity = {
            key: receipt.get(key)
            for key in ("source_dev", "source_ino", "source_size", "source_mtime_ns")
        }
        if not all(isinstance(value, int) for value in identity.values()):
            raise _provider_error("delete_import_file", "source_not_found", "导入源引用无效")
        self._reject_media_library_source(source, operation="delete_import_file")
        try:
            _reject_symlink_components(source)
            if not self._same_source_identity(source, identity):
                raise ValueError("source changed")
            source.unlink()
        except ValueError as exc:
            raise _provider_error(
                "delete_import_file", "source_not_found", "导入源已变化，未删除"
            ) from exc
        except OSError as exc:
            raise _provider_error(
                "delete_import_file", "unavailable", "导入源删除失败", retryable=True
            ) from exc

    @staticmethod
    def _operation_directory(operation_key: object) -> str:
        if not isinstance(operation_key, str) or not operation_key.strip():
            raise ValueError("invalid operation key")
        # Host keys are opaque; one component keeps them deterministic while
        # rejecting traversal and platform separators.
        parts = _relative_parts(operation_key)
        if len(parts) != 1:
            raise ValueError("unsafe operation key")
        return parts[0]

    def _journal_path(self, operation_key: object) -> Path:
        operation = self._operation_directory(operation_key)
        digest = hashlib.sha256(operation.encode("utf-8")).hexdigest()
        path = self.operation_dir / f"{digest}.json"
        _reject_symlink_components(path)
        return path

    @staticmethod
    def _source_identity(path: Path) -> dict[str, int]:
        stat = path.stat()
        return {
            "source_dev": int(stat.st_dev),
            "source_ino": int(stat.st_ino),
            "source_size": int(stat.st_size),
            "source_mtime_ns": int(stat.st_mtime_ns),
        }

    @staticmethod
    def _same_source_identity(path: Path, journal: dict[str, Any]) -> bool:
        try:
            identity = LocalStorageProvider._source_identity(path)
        except OSError:
            return False
        return all(
            identity[key] == journal.get(key)
            for key in ("source_dev", "source_ino", "source_size", "source_mtime_ns")
        )

    @staticmethod
    def _safe_relative(value: object, *, operation: str) -> str:
        try:
            return "/".join(_relative_parts(value))
        except ValueError as exc:
            raise _provider_error(operation, "source_not_found", "导入操作记录无效") from exc

    def _target_from_journal(self, journal: dict[str, Any], *, operation: str) -> Path:
        relative = self._safe_relative(journal.get("target_relative_path"), operation=operation)
        try:
            placement = ImportPlacement(relative_path=journal["placement_relative_path"])
            parts = self._placement_parts(placement)
            expected = "/".join(
                (*parts[:-1], self._operation_directory(journal["operation_key"]), parts[-1])
            )
            if relative != expected:
                raise ValueError("target does not match placement")
            path = self.media_root.joinpath(*relative.split("/"))
            _reject_symlink_components(path, include_leaf=False)
            _ensure_under(path.resolve(strict=False), self.media_root.resolve(strict=False))
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise _provider_error(operation, "source_not_found", "导入操作记录无效") from exc
        return path

    def _temporary_from_journal(self, journal: dict[str, Any], *, operation: str) -> Path:
        relative = self._safe_relative(journal.get("temp_relative_path"), operation=operation)
        target = self._target_from_journal(journal, operation=operation)
        path = self.media_root.joinpath(*relative.split("/"))
        try:
            if not path.name.startswith(".staging-") or path.parent != target.parent:
                raise ValueError("temporary path does not match target")
            _reject_symlink_components(path, include_leaf=False)
            _ensure_under(path.resolve(strict=False), self.media_root.resolve(strict=False))
        except (OSError, ValueError) as exc:
            raise _provider_error(operation, "source_not_found", "导入操作记录无效") from exc
        return path

    def _read_journal(self, operation_key: str, *, operation: str) -> dict[str, Any]:
        try:
            path = self._journal_path(operation_key)
            with path.open("r", encoding="utf-8") as handle:
                journal = json.load(handle)
        except (FileNotFoundError, ValueError) as exc:
            raise _provider_error(operation, "source_not_found", "导入回执无效") from exc
        except OSError as exc:
            raise _provider_error(operation, "unavailable", "导入操作记录不可读", retryable=True) from exc
        if not isinstance(journal, dict):
            raise _provider_error(operation, "source_not_found", "导入操作记录无效")
        if journal.get("version") != LOCAL_REF_VERSION or journal.get("kind") != _OPERATION_KIND:
            raise _provider_error(operation, "source_not_found", "导入操作记录无效")
        if journal.get("operation_key") != operation_key:
            raise _provider_error(operation, "source_not_found", "导入操作记录无效")
        if not isinstance(journal.get("token"), str) or not journal["token"]:
            raise _provider_error(operation, "source_not_found", "导入操作记录无效")
        state = journal.get("state")
        if state not in {
            _OPERATION_STATE_PREPARING,
            _OPERATION_STATE_STAGED,
            _OPERATION_STATE_FINALIZED,
            _OPERATION_STATE_ABORTED,
        }:
            raise _provider_error(operation, "source_not_found", "导入操作记录无效")
        source_ref = journal.get("source_ref")
        source_root, source_kind = self._source_root_for_ref(source_ref, operation=operation)
        _source_path, source_relative = self._path_from_ref(
            source_ref,
            root=source_root,
            operation=operation,
            expected_kind=source_kind,
        )
        if journal.get("source_relative_path") != source_relative:
            raise _provider_error(operation, "source_not_found", "导入操作记录无效")
        self._safe_relative(journal.get("placement_relative_path"), operation=operation)
        self._safe_relative(journal.get("target_relative_path"), operation=operation)
        self._safe_relative(journal.get("temp_relative_path"), operation=operation)
        if journal.get("source_disposition") not in {"keep", "delete_after_commit"}:
            raise _provider_error(operation, "source_not_found", "导入操作记录无效")
        integer_fields = ["source_dev", "source_ino", "source_size", "source_mtime_ns"]
        if state != _OPERATION_STATE_PREPARING:
            integer_fields.extend(
                ["target_size", "target_dev", "target_ino", "target_mtime_ns"]
            )
        if any(not isinstance(journal.get(field), int) for field in integer_fields):
            raise _provider_error(operation, "source_not_found", "导入操作记录无效")
        self._temporary_from_journal(journal, operation=operation)
        return journal

    def _write_journal(self, journal: dict[str, Any], *, operation: str) -> None:
        try:
            path = self._journal_path(journal["operation_key"])
            fd, temporary_name = tempfile.mkstemp(prefix=".journal-", dir=self.operation_dir)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(journal, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise _provider_error(operation, "unavailable", "导入操作记录写入失败", retryable=True) from exc

    def _receipt_journal(self, receipt: object, *, operation: str) -> dict[str, Any]:
        if not isinstance(receipt, dict) or set(receipt) != {"operation_key", "token"}:
            raise _provider_error(operation, "source_not_found", "导入回执无效")
        operation_key = receipt.get("operation_key")
        token = receipt.get("token")
        if not isinstance(operation_key, str) or not isinstance(token, str) or not token:
            raise _provider_error(operation, "source_not_found", "导入回执无效")
        journal = self._read_journal(operation_key, operation=operation)
        if not secrets.compare_digest(token, journal["token"]):
            raise _provider_error(operation, "source_not_found", "导入回执无效")
        if journal["state"] == _OPERATION_STATE_PREPARING:
            raise _provider_error(operation, "unavailable", "导入仍在准备中", retryable=True)
        self._target_from_journal(journal, operation=operation)
        return journal

    def _target_is_complete(self, target: Path, journal: dict[str, Any], *, operation: str) -> None:
        try:
            if target.is_symlink() or not target.is_file():
                raise ValueError("target is not a regular file")
            stat = target.stat()
        except (OSError, ValueError) as exc:
            raise _provider_error(operation, "source_not_found", "导入目标文件不存在") from exc
        if stat.st_size != journal["target_size"]:
            raise _provider_error(operation, "source_not_found", "导入目标文件不完整")
        if stat.st_dev != journal["target_dev"] or stat.st_ino != journal["target_ino"]:
            raise _provider_error(operation, "source_not_found", "导入目标文件已被替换")
        if stat.st_mtime_ns != journal["target_mtime_ns"]:
            raise _provider_error(operation, "source_not_found", "导入目标文件已被替换")

    @staticmethod
    def _placement_parts(placement: object) -> tuple[str, ...]:
        if not isinstance(placement, ImportPlacement):
            raise TypeError("invalid placement")
        parts = _relative_parts(placement.relative_path)
        if parts[0] not in {"jav", "videos"}:
            raise ValueError("invalid placement")
        if (parts[0] == "jav" and len(parts) != 3) or (
            parts[0] == "videos" and len(parts) != 2
        ):
            raise ValueError("invalid placement")
        return parts

    def _target_for(self, placement: ImportPlacement, operation_key: str) -> tuple[Path, str]:
        parts = self._placement_parts(placement)
        operation_directory = self._operation_directory(operation_key)
        target_parts = (*parts[:-1], operation_directory, parts[-1])
        target_relative = "/".join(target_parts)
        target = self.media_root.joinpath(*target_parts)
        try:
            _reject_symlink_components(target, include_leaf=False)
            _ensure_under(target.resolve(strict=False), self.media_root.resolve(strict=False))
        except (ValueError, OSError) as exc:
            raise _provider_error("stage_import_file", "invalid_config", "导入目标路径无效") from exc
        return target, target_relative

    def _source_for(self, source: object) -> tuple[Path, str, dict[str, int]]:
        if not isinstance(source, ImportFile):
            raise _provider_error("stage_import_file", "source_not_found", "导入源无效")
        source_root, source_kind = self._source_root_for_ref(
            source.source_ref,
            operation="stage_import_file",
        )
        path, relative = self._path_from_ref(
            source.source_ref,
            root=source_root,
            operation="stage_import_file",
            require_file=True,
            expected_kind=source_kind,
        )
        try:
            source_parts = _relative_parts(source.relative_path)
        except ValueError as exc:
            raise _provider_error("stage_import_file", "source_not_found", "导入源无效") from exc
        if "/".join(source_parts) != relative or source.name != path.name:
            raise _provider_error("stage_import_file", "source_not_found", "导入源无效")
        try:
            return path, relative, self._source_identity(path)
        except OSError as exc:
            raise _provider_error("stage_import_file", "unavailable", "导入源读取失败", retryable=True) from exc

    @staticmethod
    def _same_operation_request(
        journal: dict[str, Any],
        *,
        source_relative: str,
        source_ref: JsonObject,
        placement_relative: str,
        target_relative: str,
        source_disposition: str,
    ) -> bool:
        return all(
            journal.get(key) == value
            for key, value in {
                "source_relative_path": source_relative,
                "source_ref": source_ref,
                "placement_relative_path": placement_relative,
                "target_relative_path": target_relative,
                "source_disposition": source_disposition,
            }.items()
        )

    def _cleanup_preparing(self, journal: dict[str, Any], *, operation: str) -> None:
        target = self._target_from_journal(journal, operation=operation)
        temporary = self._temporary_from_journal(journal, operation=operation)
        for path in (temporary, target):
            try:
                if path.exists() or path.is_symlink():
                    _reject_symlink_components(path)
                    if not path.is_file():
                        raise ValueError("staging path is not a file")
                    path.unlink()
            except OSError as exc:
                raise _provider_error(operation, "unavailable", "导入暂存清理失败", retryable=True) from exc
            except ValueError as exc:
                raise _provider_error(operation, "source_not_found", "导入操作记录无效") from exc

    def stage_import_file(
        self,
        *,
        source: ImportFile,
        placement: ImportPlacement,
        source_disposition: Literal["keep", "delete_after_commit"],
        operation_key: str,
    ) -> StagedMedia:
        if not isinstance(source_disposition, str) or source_disposition not in {
            "keep",
            "delete_after_commit",
        }:
            raise _provider_error("stage_import_file", "invalid_config", "源文件处置方式无效")
        try:
            operation_name = self._operation_directory(operation_key)
        except (TypeError, ValueError) as exc:
            raise _provider_error("stage_import_file", "invalid_config", "导入操作标识无效") from exc
        source_path, source_relative, source_identity = self._source_for(source)
        metadata = MediaMetadataProbeService.probe_file(source_path)
        duration_seconds = max(0, int(metadata.duration_seconds or 0))
        resolution = getattr(metadata, "resolution", None)
        if source_disposition == "delete_after_commit":
            self._reject_media_library_source(source_path, operation="stage_import_file")
        try:
            target, target_relative = self._target_for(placement, operation_name)
        except (TypeError, ValueError) as exc:
            raise _provider_error("stage_import_file", "invalid_config", "导入目标路径无效") from exc
        try:
            existing = None
            journal_path = self._journal_path(operation_name)
            if journal_path.exists():
                existing = self._read_journal(operation_name, operation="stage_import_file")
            if existing is not None:
                if not self._same_operation_request(
                    existing,
                    source_relative=source_relative,
                    source_ref=source.source_ref,
                    placement_relative=placement.relative_path,
                    target_relative=target_relative,
                    source_disposition=source_disposition,
                ):
                    raise _provider_error("stage_import_file", "invalid_config", "导入操作标识已被占用")
                if existing["state"] == _OPERATION_STATE_PREPARING:
                    self._cleanup_preparing(existing, operation="stage_import_file")
                    journal_path.unlink(missing_ok=True)
                    existing = None
                if existing is not None and existing["state"] == _OPERATION_STATE_ABORTED:
                    raise _provider_error("stage_import_file", "invalid_config", "导入操作已终止")
                if existing is not None:
                    self._target_is_complete(target, existing, operation="stage_import_file")
                    return _staged_media(
                        storage_ref=self._media_ref(target_relative),
                        receipt={"operation_key": operation_name, "token": existing["token"]},
                        size_bytes=existing["target_size"],
                        duration_seconds=duration_seconds,
                        video_info=None,
                        resolution=resolution,
                    )
            if target.exists() or target.is_symlink():
                raise _provider_error("stage_import_file", "invalid_config", "导入目标已存在")
            target_created = False
            preserve_preparing = False
            temporary: Path | None = None
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                _reject_symlink_components(target.parent)
                fd, temporary_name = tempfile.mkstemp(prefix=".staging-", dir=target.parent)
                os.close(fd)
                temporary = Path(temporary_name)
                temporary.unlink()
                temporary_relative = _posix_relative(temporary, self.media_root)
                token = secrets.token_urlsafe(32)
                preparing_journal = {
                    "version": LOCAL_REF_VERSION,
                    "kind": _OPERATION_KIND,
                    "operation_key": operation_name,
                    "token": token,
                    "source_relative_path": source_relative,
                    "source_ref": source.source_ref,
                    "placement_relative_path": placement.relative_path,
                    "target_relative_path": target_relative,
                    "temp_relative_path": temporary_relative,
                    "source_disposition": source_disposition,
                    **source_identity,
                    "state": _OPERATION_STATE_PREPARING,
                }
                self._write_journal(preparing_journal, operation="stage_import_file")
                if source_disposition == "keep":
                    try:
                        os.link(source_path, temporary)
                    except OSError:
                        shutil.copy2(source_path, temporary)
                else:
                    shutil.copy2(source_path, temporary)
                temporary_size = temporary.stat().st_size
                if temporary_size != source_identity["source_size"]:
                    raise OSError("source changed during staging")
                os.replace(temporary, target)
                temporary = None
                target_created = True
                target_stat = self._source_identity(target)
                journal = {
                    **preparing_journal,
                    "target_size": target_stat["source_size"],
                    "target_dev": target_stat["source_dev"],
                    "target_ino": target_stat["source_ino"],
                    "target_mtime_ns": target_stat["source_mtime_ns"],
                    "state": _OPERATION_STATE_STAGED,
                }
                preserve_preparing = True
                self._write_journal(journal, operation="stage_import_file")
            except ProviderOperationError:
                if target_created and not preserve_preparing:
                    try:
                        target.unlink()
                    except OSError:
                        pass
                raise
            except (OSError, ValueError) as exc:
                if target_created and not preserve_preparing:
                    try:
                        target.unlink()
                    except OSError:
                        pass
                raise _provider_error(
                    "stage_import_file", "unavailable", "本地媒体写入失败", retryable=True
                ) from exc
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            raise _provider_error(
                "stage_import_file", "unavailable", "本地媒体写入失败", retryable=True
            ) from exc
        receipt: JsonObject = {"operation_key": operation_name, "token": token}
        return _staged_media(
            storage_ref=self._media_ref(target_relative),
            receipt=receipt,
            size_bytes=journal["target_size"],
            duration_seconds=duration_seconds,
            video_info=None,
            resolution=resolution,
        )

    def finalize_import(self, *, receipt: JsonObject) -> None:
        journal = self._receipt_journal(receipt, operation="finalize_import")
        if journal["state"] in {_OPERATION_STATE_ABORTED, _OPERATION_STATE_FINALIZED}:
            return
        target = self._target_from_journal(journal, operation="finalize_import")
        self._target_is_complete(target, journal, operation="finalize_import")
        disposition = journal["source_disposition"]
        if disposition == "delete_after_commit":
            source_ref = journal["source_ref"]
            source_root, source_kind = self._source_root_for_ref(
                source_ref,
                operation="finalize_import",
            )
            source, _ = self._path_from_ref(
                source_ref,
                root=source_root,
                operation="finalize_import",
                require_file=None,
                expected_kind=source_kind,
            )
            self._reject_media_library_source(source, operation="finalize_import")
            try:
                if source.exists() or source.is_symlink():
                    _reject_symlink_components(source)
                    if not source.is_file() or not self._same_source_identity(source, journal):
                        raise ValueError("source changed")
                    source.unlink()
            except ValueError as exc:
                raise _provider_error("finalize_import", "source_not_found", "导入源已变化，未删除") from exc
            except OSError as exc:
                raise _provider_error(
                    "finalize_import", "unavailable", "导入源删除失败", retryable=True
                ) from exc
        journal["state"] = _OPERATION_STATE_FINALIZED
        self._write_journal(journal, operation="finalize_import")

    def abort_import(self, *, receipt: JsonObject) -> None:
        journal = self._receipt_journal(receipt, operation="abort_import")
        if journal["state"] in {_OPERATION_STATE_ABORTED, _OPERATION_STATE_FINALIZED}:
            return
        target = self._target_from_journal(journal, operation="abort_import")
        try:
            if target.exists() or target.is_symlink():
                self._target_is_complete(target, journal, operation="abort_import")
                target.unlink()
        except OSError as exc:
            raise _provider_error(
                "abort_import", "unavailable", "导入暂存删除失败", retryable=True
            ) from exc
        journal["state"] = _OPERATION_STATE_ABORTED
        self._write_journal(journal, operation="abort_import")

    def delete_media(self, *, media: MediaHandle) -> None:
        path, _ = self._path_from_ref(
            media.storage_ref,
            root=self.media_root,
            operation="delete_media",
            require_file=None,
            expected_kind=MEDIA_REF_KIND,
        )
        try:
            if path.exists() or path.is_symlink():
                _reject_symlink_components(path)
                if not path.is_file():
                    raise ValueError("media is not file")
                path.unlink()
        except ValueError as exc:
            raise _provider_error("delete_media", "source_not_found", "媒体文件不可删除") from exc
        except OSError as exc:
            raise _provider_error(
                "delete_media", "unavailable", "媒体文件删除失败", retryable=True
            ) from exc

    def _media_path(self, media: MediaHandle, *, operation: str) -> Path:
        path, _ = self._path_from_ref(
            media.storage_ref,
            root=self.media_root,
            operation=operation,
            require_file=True,
            expected_kind=MEDIA_REF_KIND,
        )
        return path

    @contextmanager
    def open_transfer_source(self, *, media: MediaHandle):
        """Open one managed file as a stable, path-free transfer source.

        The descriptor is opened only once. Readers use ``pread`` with their
        own cursor, so independent range readers neither reveal nor share a
        filesystem path or descriptor offset.
        """
        path = self._media_path(media, operation="open_transfer_source")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError as exc:
            raise _provider_error("open_transfer_source", "source_not_found", "媒体文件不存在") from exc
        except OSError as exc:
            raise _provider_error(
                "open_transfer_source", "unavailable", "媒体文件读取失败", retryable=True
            ) from exc
        try:
            descriptor = os.fstat(fd)
            if not stat.S_ISREG(descriptor.st_mode):
                raise ValueError("media is not a regular file")
            snapshot = _TransferFileSnapshot.from_stat(descriptor)
            if snapshot.size != media.file_size_bytes:
                raise ValueError("media size changed")
            source = _LocalTransferSource(
                owner=self, media=media, path=path,
                fd=fd,
                snapshot=snapshot,
            )
        except ValueError as exc:
            os.close(fd)
            raise _provider_error("open_transfer_source", "source_not_found", "媒体文件不可读取") from exc
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            raise _provider_error(
                "open_transfer_source", "unavailable", "媒体文件读取失败", retryable=True
            ) from exc

        try:
            yield source
        finally:
            source._close()

    def cleanup_transfer_source(
        self, *, media: MediaHandle, source: MediaTransferSource
    ) -> None:
        operation = "cleanup_transfer_source"
        if (
            not isinstance(source, _LocalTransferSource)
            or source._owner is not self
            or source._media != media
        ):
            raise _provider_error(operation, "invalid_config", "源会话与媒体不匹配")
        try:
            source._ensure_open()
            path = self._media_path(media, operation=operation)
            if path != source._path:
                raise ValueError("source path changed")
            source.assert_unchanged()
            path.unlink()
        except (ValueError, OSError):
            raise _provider_error(
                operation, "unavailable", "源文件清理未完成"
            ) from None
        except ProviderOperationError:
            raise _provider_error(
                operation, "unavailable", "源文件身份无法确认，未完成清理"
            ) from None

    @staticmethod
    def _probe_file_duration_seconds(path: Path) -> int:
        return max(0, int(MediaMetadataProbeService.probe_file(path).duration_seconds or 0))

    @staticmethod
    def _probe_file_resolution(path: Path) -> str | None:
        resolution = getattr(MediaMetadataProbeService.probe_file(path), "resolution", None)
        return resolution if isinstance(resolution, str) else None

    @staticmethod
    def _lower_process_priority() -> None:
        try:
            os.nice(19)
        except (AttributeError, OSError):
            return

    def probe_duration_seconds(self, *, media: MediaHandle) -> int:
        return self._probe_file_duration_seconds(
            self._media_path(media, operation="probe_duration_seconds")
        )

    def probe_resolution(self, *, media: MediaHandle) -> str | None:
        return self._probe_file_resolution(
            self._media_path(media, operation="probe_resolution")
        )

    def open_cover_source(self, *, media: MediaHandle) -> BinaryIO:
        path = self._media_path(media, operation="open_cover_source")
        try:
            return path.open("rb")
        except OSError as exc:
            raise _provider_error(
                "open_cover_source", "unavailable", "媒体读取失败", retryable=True
            ) from exc

    def compute_file_hash(self, *, media: MediaHandle) -> str:
        operation = "compute_file_hash"
        size = media.file_size_bytes
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise _provider_error(operation, "invalid_config", "媒体文件大小无效")
        path = self._media_path(media, operation=operation)

        try:
            with path.open("rb") as handle:
                initial = os.fstat(handle.fileno())
                if initial.st_size != size:
                    raise ValueError("media size mismatch")

                def read_at(offset: int, length: int) -> bytes:
                    handle.seek(offset)
                    data = handle.read(length)
                    if len(data) != length:
                        raise ValueError("short read")
                    return data

                if size < _HASH_FULL_THRESHOLD:
                    full_sha1 = hashlib.sha1(read_at(0, size)).digest()
                    payload = (
                        _HASH_DOMAIN
                        + b"\x00full\x00"
                        + size.to_bytes(8, "big", signed=False)
                        + full_sha1
                    )
                else:
                    head_sha1 = hashlib.sha1(
                        read_at(0, _HASH_HEAD_TAIL_BYTES)
                    ).digest()
                    tail_sha1 = hashlib.sha1(
                        read_at(size - _HASH_HEAD_TAIL_BYTES, _HASH_HEAD_TAIL_BYTES)
                    ).digest()
                    slot_count = (
                        size - 2 * _HASH_HEAD_TAIL_BYTES
                    ) // _HASH_MIDDLE_BYTES
                    head_seed = int.from_bytes(head_sha1[:8], "big")
                    tail_seed = int.from_bytes(tail_sha1[:8], "big")
                    slot_1 = head_seed % slot_count
                    candidate = tail_seed % (slot_count - 1)
                    slot_2 = candidate if candidate < slot_1 else candidate + 1
                    middle_1_offset = (
                        _HASH_HEAD_TAIL_BYTES + slot_1 * _HASH_MIDDLE_BYTES
                    )
                    middle_2_offset = (
                        _HASH_HEAD_TAIL_BYTES + slot_2 * _HASH_MIDDLE_BYTES
                    )
                    middle_1_sha1 = hashlib.sha1(
                        read_at(middle_1_offset, _HASH_MIDDLE_BYTES)
                    ).digest()
                    middle_2_sha1 = hashlib.sha1(
                        read_at(middle_2_offset, _HASH_MIDDLE_BYTES)
                    ).digest()
                    payload = (
                        _HASH_DOMAIN
                        + b"\x00sampled\x00"
                        + size.to_bytes(8, "big", signed=False)
                        + head_sha1
                        + tail_sha1
                        + middle_1_sha1
                        + middle_2_sha1
                    )

                final_stat = os.fstat(handle.fileno())
                if (
                    final_stat.st_dev,
                    final_stat.st_ino,
                    final_stat.st_size,
                    final_stat.st_mtime_ns,
                ) != (
                    initial.st_dev,
                    initial.st_ino,
                    initial.st_size,
                    initial.st_mtime_ns,
                ):
                    raise ValueError("media changed during hashing")
        except FileNotFoundError as exc:
            raise _provider_error(operation, "source_not_found", "媒体文件不存在") from exc
        except (OSError, ValueError) as exc:
            raise _provider_error(
                operation, "unavailable", "媒体文件读取失败", retryable=True
            ) from exc

        return f"media-file-hash-v1:{hashlib.sha1(payload).hexdigest()}"

    @staticmethod
    def _workspace_path(workspace: Path, *, operation: str) -> Path:
        try:
            raw = Path(workspace).expanduser()
            if not raw.is_absolute():
                raw = Path.cwd() / raw
            _reject_symlink_components(raw)
            resolved = raw.resolve(strict=False)
            resolved.mkdir(parents=True, exist_ok=True)
            _reject_symlink_components(resolved)
            return resolved
        except (OSError, ValueError) as exc:
            raise _provider_error(operation, "unavailable", "工作目录不可用", retryable=True) from exc

    @staticmethod
    def _parse_range(range_header: str, size: int) -> tuple[int, int] | None:
        match = _RANGE_RE.fullmatch(range_header.strip())
        if match is None or size == 0:
            return None
        start_text, end_text = match.groups()
        try:
            if not start_text:
                suffix = int(end_text)
                if suffix <= 0:
                    raise ValueError
                return max(0, size - suffix), size - 1
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
            if start >= size or end < start:
                raise ValueError
            return start, min(end, size - 1)
        except ValueError:
            return None

    async def handle_playback(self, *, media: MediaHandle, context: PlaybackContext) -> Response:
        if context.resource_path not in {"", None}:
            raise _provider_error("playback", "unsupported", "本地存储不支持媒体子资源")
        path = self._media_path(media, operation="playback")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        range_header = context.request.headers.get("range")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise _provider_error("playback", "unavailable", "媒体读取失败", retryable=True) from exc
        status_code = 200
        start, end = 0, size - 1
        length = size
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(size)}
        if range_header:
            requested = self._parse_range(range_header, size)
            if requested is None:
                return Response(
                    status_code=416,
                    media_type=media_type,
                    headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{size}"},
            )
            start, end = requested
            status_code = 206
            length = end - start + 1
            headers.update(
                {
                    "Content-Length": str(length),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                }
            )
        if context.request.method.upper() == "HEAD":
            return Response(status_code=status_code, media_type=media_type, headers=headers)
        def stream():
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            stream(),
            status_code=status_code,
            media_type=media_type,
            headers=headers,
        )

    def _build_merged_layout(self, *, medias: tuple[MediaHandle, ...]):
        try:
            entries = [
                (media.media_id, self._media_path(media, operation="merged_playback"))
                for media in medias
            ]
            return build_layout(entries)
        except Mp4MergeError as exc:
            raise _provider_error("merged_playback", "unsupported", exc.message) from exc
        except OSError as exc:
            raise _provider_error(
                "merged_playback", "unavailable", "媒体文件读取失败", retryable=True
            ) from exc

    def preflight_merged_playback(self, *, medias: tuple[MediaHandle, ...]) -> None:
        self._build_merged_layout(medias=medias)

    async def handle_merged_playback(
        self,
        *,
        medias: tuple[MediaHandle, ...],
        context: PlaybackContext,
    ) -> Response:
        if context.resource_path != "stream.mp4":
            raise _provider_error("merged_playback", "source_not_found", "本地合并播放资源不存在")
        layout = await asyncio.to_thread(self._build_merged_layout, medias=medias)
        return merged_range_requests_response(context.request, layout, "video/mp4")

    def generate_thumbnails(self, *, media: MediaHandle, workspace: Path) -> ThumbnailGeneration:
        source = self._media_path(media, operation="generate_thumbnails")
        workspace = self._workspace_path(workspace, operation="generate_thumbnails")
        try:
            import av
            from PIL import Image
        except ImportError as exc:
            raise _provider_error(
                "generate_thumbnails", "unavailable", "缩略图组件不可用", retryable=True
            ) from exc

        interval_seconds = 10
        duration_seconds = max(0, int(media.duration_seconds or 0))
        artifacts: list[ThumbnailArtifact] = []

        def write_thumbnail(frame: Any, offset: int) -> None:
            relative = f"thumbnail-{offset}.webp"
            destination = workspace / relative
            _reject_symlink_components(destination)
            temporary: Path | None = None
            image = None
            try:
                fd, temporary_name = tempfile.mkstemp(prefix=".thumbnail-", dir=workspace)
                os.close(fd)
                temporary = Path(temporary_name)
                image = frame.to_image()
                image.thumbnail((640, 360), Image.Resampling.LANCZOS)
                image.save(temporary, format="WEBP", quality=82, method=4)
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    raise ValueError("empty thumbnail")
                os.replace(temporary, destination)
                temporary = None
                artifacts.append(ThumbnailArtifact(offset_seconds=offset, relative_path=relative))
            except ProviderOperationError:
                raise
            except Exception as exc:
                raise _provider_error(
                    "generate_thumbnails", "unavailable", "缩略图生成失败", retryable=True
                ) from exc
            finally:
                if image is not None:
                    image.close()
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

        try:
            self._lower_process_priority()
            with av.open(str(source)) as container:
                video_streams = getattr(container.streams, "video", ())
                if not video_streams:
                    raise _provider_error(
                        "generate_thumbnails", "unavailable", "视频流不存在", retryable=True
                    )
                stream = video_streams[0]
                if duration_seconds <= 0:
                    stream_duration = getattr(stream, "duration", None)
                    stream_time_base = getattr(stream, "time_base", None)
                    if stream_duration is not None and stream_time_base:
                        duration_seconds = max(0, int(float(stream_duration * stream_time_base)))
                    if duration_seconds <= 0:
                        container_duration = getattr(container, "duration", None)
                        av_time_base = getattr(av, "time_base", None)
                        if container_duration is not None and av_time_base:
                            duration_seconds = max(
                                0, int(float(container_duration / av_time_base))
                            )
                if duration_seconds <= 0:
                    return ThumbnailGeneration(expected_count=0, artifacts=())

                for offset in range(0, duration_seconds + 1, interval_seconds):
                    try:
                        if offset == 0:
                            frame = next(container.decode(stream))
                        else:
                            time_base = float(stream.time_base or 0)
                            if time_base <= 0:
                                raise ValueError("video stream time base is missing")
                            timestamp = int(offset / time_base)
                            container.seek(
                                timestamp,
                                stream=stream,
                                backward=True,
                                any_frame=False,
                            )
                            frame = next(container.decode(stream))
                        write_thumbnail(frame, offset)
                    except StopIteration:
                        logger.warning(
                            "Local thumbnail frame missing media_id=%s offset_seconds=%s",
                            media.media_id,
                            offset,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Local thumbnail frame failed media_id=%s offset_seconds=%s detail=%s",
                            media.media_id,
                            offset,
                            exc,
                        )
        except ProviderOperationError:
            raise
        except Exception as exc:
            raise _provider_error(
                "generate_thumbnails", "unavailable", "视频解码失败", retryable=True
            ) from exc
        if not artifacts:
            raise _provider_error("generate_thumbnails", "source_not_found", "视频没有可用画面")
        expected_count = max(1, duration_seconds // interval_seconds + 1)
        return ThumbnailGeneration(expected_count=expected_count, artifacts=tuple(artifacts))

    def create_clip(
        self,
        *,
        media: MediaHandle,
        start_offset_seconds: int,
        end_offset_seconds: int,
        workspace: Path,
    ) -> ClipArtifact:
        if (
            not isinstance(start_offset_seconds, int)
            or not isinstance(end_offset_seconds, int)
            or start_offset_seconds < 0
            or end_offset_seconds <= start_offset_seconds
        ):
            raise _provider_error("create_clip", "invalid_config", "片段时间范围无效")
        source = self._media_path(media, operation="create_clip")
        workspace = self._workspace_path(workspace, operation="create_clip")
        destination = workspace / "clip.mp4"
        temporary: Path | None = None
        try:
            _reject_symlink_components(destination)
            _ensure_under(destination.resolve(strict=False), workspace)
            fd, temporary_name = tempfile.mkstemp(prefix=".clip-", suffix=".mp4", dir=workspace)
            os.close(fd)
            temporary = Path(temporary_name)
            temporary.unlink()
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    str(start_offset_seconds),
                    "-to",
                    str(end_offset_seconds),
                    "-i",
                    str(source),
                    "-c",
                    "copy",
                    "-avoid_negative_ts",
                    "make_zero",
                    "-y",
                    str(temporary),
                ],
                check=True,
                capture_output=True,
                timeout=300,
            )
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise ValueError("empty clip")
            os.replace(temporary, destination)
            temporary = None
        except FileNotFoundError as exc:
            raise _provider_error(
                "create_clip", "unavailable", "视频剪辑组件不可用", retryable=True
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise _provider_error(
                "create_clip", "unavailable", "视频剪辑超时", retryable=True
            ) from exc
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise _provider_error(
                "create_clip", "unavailable", "视频剪辑失败", retryable=True
            ) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return ClipArtifact(relative_path="clip.mp4")
