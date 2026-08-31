import asyncio

import pytest
from fastapi import HTTPException
from sakuramedia_local_provider.merged_playback import merged_range_requests_response
from starlette.requests import Request


class _MemoryLayout:
    def __init__(self, data: bytes) -> None:
        self._data = data

    @property
    def total_size(self) -> int:
        return len(self._data)

    def resolve_range(self, start: int, end: int):
        return [("mem", self._data[start:end], 0, 0)]


def _request(range_header: str | None = None) -> Request:
    headers = [] if range_header is None else [(b"range", range_header.encode())]
    return Request({"type": "http", "method": "GET", "headers": headers})


async def _body(response) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


def test_merged_range_response_returns_full_layout() -> None:
    response = merged_range_requests_response(
        _request(), _MemoryLayout(b"abcdefghij"), "video/mp4"
    )

    assert response.status_code == 200
    assert response.headers["content-length"] == "10"
    assert asyncio.run(_body(response)) == b"abcdefghij"


def test_merged_range_response_returns_requested_range() -> None:
    response = merged_range_requests_response(
        _request("bytes=2-5"), _MemoryLayout(b"abcdefghij"), "video/mp4"
    )

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert asyncio.run(_body(response)) == b"cdef"


def test_merged_range_response_supports_suffix_range() -> None:
    response = merged_range_requests_response(
        _request("bytes=-3"), _MemoryLayout(b"abcdefghij"), "video/mp4"
    )

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 7-9/10"
    assert asyncio.run(_body(response)) == b"hij"


def test_merged_range_response_rejects_invalid_range() -> None:
    with pytest.raises(HTTPException) as error:
        merged_range_requests_response(
            _request("bytes=10-11"), _MemoryLayout(b"abcdefghij"), "video/mp4"
        )

    assert error.value.status_code == 416
