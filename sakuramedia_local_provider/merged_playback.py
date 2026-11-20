"""Local virtual-MP4 merge layout cache."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from pathlib import Path

from fastapi import HTTPException, status
from starlette.requests import Request
from starlette.responses import StreamingResponse

from .merged_mp4 import (
    MergedLayout,
    Mp4MergeError,
    build_merged_layout,
    parse_file,
)

_CACHE_TTL_SECONDS = 300
_cache: dict[tuple[tuple[int, str, int, int], ...], tuple[float, MergedLayout]] = {}
_cache_lock = threading.Lock()


def _cache_key(entries: list[tuple[int, Path]]) -> tuple[tuple[int, str, int, int], ...]:
    keys: list[tuple[int, str, int, int]] = []
    for media_id, path in entries:
        stat = path.stat()
        keys.append((media_id, str(path), stat.st_size, stat.st_mtime_ns))
    return tuple(keys)


def _cleanup_cache(now: float) -> None:
    stale_keys = [
        key for key, (built_at, _layout) in _cache.items() if now - built_at > _CACHE_TTL_SECONDS
    ]
    for key in stale_keys:
        _cache.pop(key, None)


def build_layout(entries: list[tuple[int, Path]]) -> MergedLayout:
    with _cache_lock:
        if len(entries) < 2:
            raise Mp4MergeError(
                "合并播放至少需要 2 个分段",
                error_code="merged_mp4_need_at_least_two",
            )
        cache_key = _cache_key(entries)
        now = time.monotonic()
        _cleanup_cache(now)
        cached = _cache.get(cache_key)
        if cached is not None and now - cached[0] <= _CACHE_TTL_SECONDS:
            return cached[1]

        layout = build_merged_layout([parse_file(str(path)) for _media_id, path in entries])
        _cache[cache_key] = (now, layout)
        return layout


def _get_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    def _invalid_range() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail=f"Invalid request range (Range:{range_header!r})",
        )

    try:
        start_text, end_text = range_header.replace("bytes=", "", 1).split("-", 1)
        if start_text == "":
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise _invalid_range()
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
    except ValueError as exc:
        raise _invalid_range() from exc

    if start > end or start < 0 or end > file_size - 1:
        raise _invalid_range()
    return start, end


def _send_merged_bytes_range_requests(
    layout: MergedLayout, start: int, end: int, chunk_size: int = 65_536
) -> Iterable[bytes]:
    for kind, arg, offset, length in layout.resolve_range(start, end + 1):
        if kind == "mem":
            data: bytes = arg
            pos = 0
            while pos < len(data):
                yield data[pos:pos + chunk_size]
                pos += chunk_size
            continue

        path: str = arg
        with open(path, mode="rb") as stream:
            stream.seek(offset)
            remaining = length
            while remaining > 0:
                chunk = stream.read(min(chunk_size, remaining))
                if not chunk:
                    break
                yield chunk
                remaining -= len(chunk)


def merged_range_requests_response(
    request: Request, layout: MergedLayout, content_type: str
) -> StreamingResponse:
    total_size = layout.total_size
    range_header = request.headers.get("range")

    headers = {
        "content-type": content_type,
        "accept-ranges": "bytes",
        "content-encoding": "identity",
        "content-length": str(total_size),
        "access-control-expose-headers": (
            "content-type, accept-ranges, content-length, "
            "content-range, content-encoding"
        ),
    }
    start = 0
    end = total_size - 1
    status_code = status.HTTP_200_OK

    if range_header is not None:
        start, end = _get_range_header(range_header, total_size)
        size = end - start + 1
        headers["content-length"] = str(size)
        headers["content-range"] = f"bytes {start}-{end}/{total_size}"
        status_code = status.HTTP_206_PARTIAL_CONTENT

    return StreamingResponse(
        _send_merged_bytes_range_requests(layout, start, end),
        headers=headers,
        status_code=status_code,
    )
