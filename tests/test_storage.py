from __future__ import annotations

import asyncio
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
import sakuramedia_local_provider.storage as storage_module
from sakuramedia_local_provider.storage import LocalStorageProvider
from starlette.requests import Request

from src.plugins.provider_protocol import (
    ImportPlacement,
    LibraryHandle,
    MediaHandle,
    PlaybackContext,
    ProviderOperationError,
)


async def _response_body(response) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


def _provider(tmp_path: Path) -> tuple[LocalStorageProvider, LibraryHandle, Path, Path]:
    media_root = tmp_path / "media"
    import_root = tmp_path / "imports"
    library = LibraryHandle(
        library_id=1,
        provider_key="local",
        provider_config={
            "media_root_path": str(media_root),
            "manual_import_root_path": str(import_root),
        },
        account_key=None,
    )
    return LocalStorageProvider(library=library, data_dir=tmp_path / "plugin-data"), library, media_root, import_root


def _media(
    library: LibraryHandle,
    relative_path: str,
    *,
    media_id: int = 1,
    duration: int = 20,
    file_size_bytes: int = 0,
) -> MediaHandle:
    return MediaHandle(
        media_id=media_id,
        library=library,
        storage_ref={"version": 1, "kind": "media_local_path", "relative_path": relative_path},
        file_name=Path(relative_path).name,
        file_size_bytes=file_size_bytes,
        duration_seconds=duration,
    )


def _hash_fixture(size: int) -> bytes:
    return bytes(
        ((((1_103_515_245 * i + 12_345) % (2**32)) >> 24) & 0xFF)
        for i in range(size)
    )


def test_browse_scan_refs_are_relative_and_symlinks_are_ignored(tmp_path: Path) -> None:
    provider, _library, _media_root, import_root = _provider(tmp_path)
    (import_root / "nested").mkdir(parents=True)
    (import_root / "nested" / "clip.mp4").write_bytes(b"video")
    (import_root / "notes.txt").write_text("notes", encoding="utf-8")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    try:
        (import_root / "escape.mp4").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    page = provider.browse(parent_ref=None, cursor=None, limit=20)
    assert [entry.name for entry in page.entries] == ["nested", "notes.txt"]
    assert all(not str(entry.source_ref).startswith(str(tmp_path)) for entry in page.entries)

    nested = page.entries[0]
    files = provider.scan_import_source(source_ref=nested.source_ref)
    assert [item.relative_path for item in files] == ["nested/clip.mp4"]
    assert files[0].source_ref == {
        "version": 1,
        "kind": "manual_local_path",
        "relative_path": "nested/clip.mp4",
    }


def test_scan_managed_media_ref_keys_lists_regular_files_and_ignores_symlinks(
    tmp_path: Path,
) -> None:
    provider, _library, media_root, _import_root = _provider(tmp_path)
    nested = media_root / "jav" / "ABC-001"
    nested.mkdir(parents=True)
    (nested / "movie.mp4").write_bytes(b"movie")
    (media_root / "notes.txt").write_text("notes", encoding="utf-8")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "hidden.mp4").write_bytes(b"hidden")
    try:
        (media_root / "linked.mp4").symlink_to(outside)
        (media_root / "linked-dir").symlink_to(outside_dir, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    keys = provider.scan_managed_media_ref_keys()

    assert keys == {"jav/ABC-001/movie.mp4", "notes.txt"}


def test_managed_media_ref_key_uses_relative_path(tmp_path: Path) -> None:
    provider, _library, _media_root, _import_root = _provider(tmp_path)

    key = provider.managed_media_ref_key(
        media_ref={
            "version": 1,
            "kind": "media_local_path",
            "relative_path": "nested/movie.mp4",
        }
    )

    assert key == "nested/movie.mp4"


def test_open_transfer_source_exposes_independent_pathless_seekable_readers(tmp_path: Path) -> None:
    provider, library, media_root, _import_root = _provider(tmp_path)
    path = media_root / "nested" / "movie.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"0123456789")

    with provider.open_transfer_source(
        media=_media(library, "nested/movie.mp4", file_size_bytes=10)
    ) as source:
        assert source.info.file_name == "movie.mp4"
        assert source.info.size_bytes == 10
        with source.open_reader() as first, source.open_reader() as second:
            assert not hasattr(first, "name")
            assert not hasattr(first, "fileno")
            assert not hasattr(first, "close")
            assert first.read(3) == b"012"
            assert second.read(2) == b"01"
            assert first.seek(7) == 7
            assert first.seek(0, 1) == 7
            assert first.read() == b"789"
            assert second.read() == b"23456789"
        source.assert_unchanged()


def test_open_transfer_source_keeps_original_inode_but_rejects_path_replacement(
    tmp_path: Path,
) -> None:
    provider, library, media_root, _import_root = _provider(tmp_path)
    path = media_root / "movie.mp4"
    path.write_bytes(b"original")
    replacement = media_root / "replacement.mp4"
    replacement.write_bytes(b"new-file")

    with provider.open_transfer_source(
        media=_media(library, "movie.mp4", file_size_bytes=8)
    ) as source:
        replacement.replace(path)
        with source.open_reader() as reader:
            assert reader.read() == b"original"
        with pytest.raises(ProviderOperationError) as error:
            source.assert_unchanged()

    assert error.value.operation == "open_transfer_source"
    assert error.value.code == "source_not_found"
    assert error.value.safe_message == "媒体文件已变化"


def test_open_transfer_source_rejects_in_place_file_changes(tmp_path: Path) -> None:
    provider, library, media_root, _import_root = _provider(tmp_path)
    path = media_root / "movie.mp4"
    path.write_bytes(b"original")

    with provider.open_transfer_source(
        media=_media(library, "movie.mp4", file_size_bytes=8)
    ) as source:
        path.write_bytes(b"changed!")
        with pytest.raises(ProviderOperationError) as error:
            source.assert_unchanged()

    assert error.value.operation == "open_transfer_source"
    assert error.value.code == "source_not_found"


def test_open_transfer_source_rejects_stale_media_size(tmp_path: Path) -> None:
    provider, library, media_root, _import_root = _provider(tmp_path)
    (media_root / "movie.mp4").write_bytes(b"content")

    with (
        pytest.raises(ProviderOperationError) as error,
        provider.open_transfer_source(
            media=_media(library, "movie.mp4", file_size_bytes=99)
        ),
    ):
        pass

    assert error.value.operation == "open_transfer_source"
    assert error.value.code == "source_not_found"


def test_open_transfer_source_rejects_symlink_media_ref(tmp_path: Path) -> None:
    provider, library, media_root, _import_root = _provider(tmp_path)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    try:
        (media_root / "linked.mp4").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with (
        pytest.raises(ProviderOperationError) as error,
        provider.open_transfer_source(media=_media(library, "linked.mp4")),
    ):
        pass

    assert error.value.operation == "open_transfer_source"
    assert error.value.code == "source_not_found"


def test_import_source_identity_tracks_source_content_and_location(
    tmp_path: Path,
) -> None:
    provider, _library, _media_root, import_root = _provider(tmp_path)
    source_path = import_root / "clip.mp4"
    source_path.write_bytes(b"source")
    root_ref = {"version": 1, "kind": "manual_local_path", "relative_path": ""}

    source = provider.scan_import_source(source_ref=root_ref)[0]
    identity = provider.get_import_source_identity(source=source)
    assert (
        provider.get_import_source_identity(
            source=provider.scan_import_source(source_ref=root_ref)[0]
        )
        == identity
    )

    source_path.write_bytes(b"changed source")
    changed_identity = provider.get_import_source_identity(
        source=provider.scan_import_source(source_ref=root_ref)[0]
    )
    assert changed_identity != identity

    source_path.rename(import_root / "renamed.mp4")
    assert (
        provider.get_import_source_identity(
            source=provider.scan_import_source(source_ref=root_ref)[0]
        )
        != changed_identity
    )


def test_stage_is_idempotent_and_layout_has_operation_version(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        storage_module.MediaMetadataProbeService,
        "probe_file",
        lambda _path: SimpleNamespace(duration_seconds=42, resolution="720x1280"),
    )
    provider, library, media_root, import_root = _provider(tmp_path)
    source_path = import_root / "ABC-001.mp4"
    source_path.write_bytes(b"source")
    source = provider.scan_import_source(
        source_ref={"version": 1, "kind": "manual_local_path", "relative_path": ""}
    )[0]
    placement = ImportPlacement(relative_path="jav/ABC-001/ABC-001.mp4")

    first = provider.stage_import_file(
        source=source,
        placement=placement,
        source_disposition="keep",
        operation_key="import-1",
    )
    second = provider.stage_import_file(
        source=source,
        placement=placement,
        source_disposition="keep",
        operation_key="import-1",
    )
    assert first.storage_ref == second.storage_ref
    assert first.receipt == second.receipt
    assert first.duration_seconds == 42
    assert second.duration_seconds == 42
    assert first.resolution == "720x1280"
    assert second.resolution == "720x1280"
    target = media_root / "jav/ABC-001/import-1/ABC-001.mp4"
    assert target.read_bytes() == b"source"
    assert os.stat(target).st_ino == os.stat(source_path).st_ino

    newer = provider.stage_import_file(
        source=source,
        placement=placement,
        source_disposition="keep",
        operation_key="import-2",
    )
    assert newer.storage_ref != first.storage_ref
    assert (media_root / "jav/ABC-001/import-2/ABC-001.mp4").is_file()
    assert provider.probe_duration_seconds(
        media=_media(library, first.storage_ref["relative_path"])
    ) == 42
    assert provider.probe_resolution(
        media=_media(library, first.storage_ref["relative_path"])
    ) == "720x1280"
    provider.delete_media(media=_media(library, first.storage_ref["relative_path"]))
    assert not target.exists()
    provider.delete_media(media=_media(library, first.storage_ref["relative_path"]))


def test_stage_supports_legacy_staged_media_contract(tmp_path: Path, monkeypatch) -> None:
    @dataclass
    class LegacyStagedMedia:
        storage_ref: dict
        receipt: dict
        size_bytes: int
        duration_seconds: int | None
        video_info: dict | None

    monkeypatch.setattr(storage_module, "StagedMedia", LegacyStagedMedia)
    monkeypatch.setattr(
        storage_module.MediaMetadataProbeService,
        "probe_file",
        lambda _path: SimpleNamespace(duration_seconds=42, resolution="720x1280"),
    )
    provider, _library, _media_root, import_root = _provider(tmp_path)
    (import_root / "clip.mp4").write_bytes(b"source")
    source = provider.scan_import_source(
        source_ref={"version": 1, "kind": "manual_local_path", "relative_path": ""}
    )[0]
    placement = ImportPlacement(relative_path="jav/ABC-001/clip.mp4")

    first = provider.stage_import_file(
        source=source,
        placement=placement,
        source_disposition="keep",
        operation_key="legacy-import",
    )
    second = provider.stage_import_file(
        source=source,
        placement=placement,
        source_disposition="keep",
        operation_key="legacy-import",
    )

    assert isinstance(first, LegacyStagedMedia)
    assert isinstance(second, LegacyStagedMedia)
    assert first.duration_seconds == second.duration_seconds == 42
    assert not hasattr(first, "resolution")


def test_delete_after_commit_copies_then_only_deletes_source(tmp_path: Path) -> None:
    provider, _library, media_root, import_root = _provider(tmp_path)
    source_path = import_root / "video.mp4"
    source_path.write_bytes(b"source")
    source = provider.scan_import_source(
        source_ref={"version": 1, "kind": "manual_local_path", "relative_path": "video.mp4"}
    )[0]
    staged = provider.stage_import_file(
        source=source,
        placement=ImportPlacement(relative_path="videos/video.mp4"),
        source_disposition="delete_after_commit",
        operation_key="delete-1",
    )
    target = media_root / "videos/delete-1/video.mp4"
    assert target.read_bytes() == source_path.read_bytes()
    assert os.stat(target).st_ino != os.stat(source_path).st_ino
    provider.finalize_import(receipt=staged.receipt)
    provider.finalize_import(receipt=staged.receipt)
    assert not source_path.exists()
    assert target.exists()
    assert (media_root / "videos/delete-1").is_dir()


def test_delete_after_commit_rejects_source_inside_media_root(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    library = LibraryHandle(
        library_id=1,
        provider_key="local",
        provider_config={
            "media_root_path": str(media_root),
            "manual_import_root_path": str(tmp_path),
        },
        account_key=None,
    )
    provider = LocalStorageProvider(library=library, data_dir=tmp_path / "plugin-data")
    source_path = media_root / "existing.mp4"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"existing")
    source = provider.scan_import_source(
        source_ref={"version": 1, "kind": "manual_local_path", "relative_path": "media/existing.mp4"}
    )[0]

    with pytest.raises(ProviderOperationError) as error:
        provider.stage_import_file(
            source=source,
            placement=ImportPlacement(relative_path="videos/existing.mp4"),
            source_disposition="delete_after_commit",
            operation_key="protected-source",
        )

    assert error.value.code == "unsupported"
    assert source_path.read_bytes() == b"existing"


def test_read_and_delete_import_file_uses_identity_receipt(tmp_path: Path) -> None:
    provider, _library, _media_root, import_root = _provider(tmp_path)
    source_path = import_root / "ABC-001.srt"
    source_path.write_bytes(b"subtitle")
    source = provider.scan_import_source(
        source_ref={"version": 1, "kind": "manual_local_path", "relative_path": "ABC-001.srt"}
    )[0]

    imported = provider.read_import_file(source=source)
    assert imported.content == b"subtitle"
    provider.delete_import_file(receipt=imported.deletion_receipt)
    assert not source_path.exists()


def test_download_source_ref_uses_its_backend_root(tmp_path: Path) -> None:
    provider, _library, media_root, _manual_root = _provider(tmp_path)
    download_root = tmp_path / "qb-downloads"
    source_path = download_root / "ABC-001" / "ABC-001.mp4"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"downloaded")

    source = provider.scan_import_source(
        source_ref={
            "version": 1,
            "kind": "download_local_path",
            "root_path": str(download_root),
            "relative_path": "ABC-001",
        }
    )[0]
    staged = provider.stage_import_file(
        source=source,
        placement=ImportPlacement(relative_path="jav/ABC-001/ABC-001.mp4"),
        source_disposition="keep",
        operation_key="download-1",
    )

    target = media_root / "jav/ABC-001/download-1/ABC-001.mp4"
    assert staged.storage_ref["kind"] == "media_local_path"
    assert target.read_bytes() == b"downloaded"
    assert os.stat(target).st_ino == os.stat(source_path).st_ino


def test_playback_supports_one_range_and_rejects_escape(tmp_path: Path) -> None:
    provider, library, media_root, _import_root = _provider(tmp_path)
    path = media_root / "videos/clip.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"0123456789")
    media = _media(library, "videos/clip.mp4")

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"range", b"bytes=2-5")],
        }
    )
    response = asyncio.run(
        provider.handle_playback(
            media=media,
            context=PlaybackContext(
                request=request,
                resource_path="",
                delivery="proxy",
                url_for=lambda value: value,
            ),
        )
    )
    assert response.status_code == 206
    assert asyncio.run(_response_body(response)) == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"

    with pytest.raises(ProviderOperationError) as error:
        provider.delete_media(
            media=_media(library, "../outside.mp4"),
        )
    assert "/" not in error.value.safe_message
    assert str(tmp_path) not in error.value.safe_message


def test_merged_playback_uses_ordered_local_media_and_range_response(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider, library, media_root, _import_root = _provider(tmp_path)
    first_path = media_root / "videos/part-1.mp4"
    second_path = media_root / "videos/part-2.mp4"
    first_path.parent.mkdir(parents=True)
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    seen: dict[str, object] = {}

    class Layout:
        total_size = 10

        @staticmethod
        def resolve_range(start: int, end: int):
            return [("mem", b"0123456789"[start:end], 0, 0)]

    def build_fake_layout(entries):
        seen["entries"] = entries
        seen["thread_id"] = threading.get_ident()
        return Layout()

    monkeypatch.setattr(storage_module, "build_layout", build_fake_layout)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"range", b"bytes=2-5")],
        }
    )
    response = asyncio.run(
        provider.handle_merged_playback(
            medias=(
                _media(library, "videos/part-1.mp4", media_id=1),
                _media(library, "videos/part-2.mp4", media_id=2),
            ),
            context=PlaybackContext(
                request=request,
                resource_path="stream.mp4",
                delivery="proxy",
                url_for=lambda value: value,
            ),
        )
    )

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert asyncio.run(_response_body(response)) == b"2345"
    assert seen["entries"] == [(1, first_path), (2, second_path)]
    assert seen["thread_id"] != threading.get_ident()


def test_merged_playback_preflight_rejects_unsupported_layout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider, library, media_root, _import_root = _provider(tmp_path)
    first_path = media_root / "videos/part-1.mp4"
    second_path = media_root / "videos/part-2.mp4"
    first_path.parent.mkdir(parents=True)
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")

    def reject_layout(_entries):
        raise storage_module.Mp4MergeError("分段视频规格不一致，不支持合并播放")

    monkeypatch.setattr(storage_module, "build_layout", reject_layout)

    with pytest.raises(ProviderOperationError) as error:
        provider.preflight_merged_playback(
            medias=(
                _media(library, "videos/part-1.mp4", media_id=1),
                _media(library, "videos/part-2.mp4", media_id=2),
            )
        )

    assert error.value.operation == "merged_playback"
    assert error.value.code == "unsupported"
    assert error.value.safe_message == "分段视频规格不一致，不支持合并播放"


def test_compute_file_hash_matches_protocol_vector(tmp_path: Path) -> None:
    provider, library, media_root, _import_root = _provider(tmp_path)
    path = media_root / "videos/hash.bin"
    path.parent.mkdir(parents=True)
    data = _hash_fixture(8 * 1024 * 1024)
    path.write_bytes(data)

    assert provider.compute_file_hash(
        media=_media(library, "videos/hash.bin", file_size_bytes=len(data))
    ) == "media-file-hash-v1:52385d3512a8a9ff8b6e6c5aa315e46633b28d9a"


@pytest.mark.parametrize(
    ("content", "expected"),
    (
        (
            b"",
            "media-file-hash-v1:524935ebf533f3b952f2397f80691a87a7b289c7",
        ),
        (
            b"abc",
            "media-file-hash-v1:da6ba51927337cc1035be69e84f851f48dbe7d71",
        ),
    ),
)
def test_compute_file_hash_uses_full_hash_for_small_files(
    tmp_path: Path, content: bytes, expected: str
) -> None:
    provider, library, media_root, _import_root = _provider(tmp_path)
    path = media_root / "videos/small.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    assert provider.compute_file_hash(
        media=_media(library, "videos/small.bin", file_size_bytes=len(content))
    ) == expected


def test_compute_file_hash_rejects_stale_media_size(tmp_path: Path) -> None:
    provider, library, media_root, _import_root = _provider(tmp_path)
    path = media_root / "videos/hash.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"abc")

    with pytest.raises(ProviderOperationError) as error:
        provider.compute_file_hash(
            media=_media(library, "videos/hash.bin", file_size_bytes=4)
        )

    assert error.value.code == "unavailable"


def test_open_cover_source_returns_a_seekable_media_file(tmp_path: Path) -> None:
    provider, library, media_root, _import_root = _provider(tmp_path)
    path = media_root / "videos/clip.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"media")

    with provider.open_cover_source(media=_media(library, "videos/clip.mp4")) as source:
        assert source.seekable()
        assert source.read() == b"media"


def test_thumbnails_seek_each_offset_and_write_webp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, library, media_root, _import_root = _provider(tmp_path)
    path = media_root / "videos/clip.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"placeholder")

    from PIL import Image

    class FakeFrame:
        def __init__(self, time: float):
            self.time = time
            self.pts = None

        def to_image(self):
            return Image.new("RGB", (16, 16), "red")

    class FakeContainer:
        stream = SimpleNamespace(type="video", time_base=1)
        streams = SimpleNamespace(video=(stream,))

        def __init__(self):
            self.seek_calls = []
            self.decode_calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def seek(self, timestamp, *, stream, backward, any_frame):
            assert stream is self.stream
            assert backward is True
            assert any_frame is False
            self.seek_calls.append(timestamp)

        def decode(self, stream):
            assert stream is self.stream
            self.decode_calls += 1
            return iter((FakeFrame(0),))

    container = FakeContainer()
    fake_av = SimpleNamespace(open=lambda _path: container)
    monkeypatch.setitem(sys.modules, "av", fake_av)
    monkeypatch.setattr(storage_module.os, "nice", lambda _value: 0)
    workspace = tmp_path / "thumbs"
    generation = provider.generate_thumbnails(
        media=_media(library, "videos/clip.mp4", duration=20), workspace=workspace
    )
    assert generation.expected_count == 3
    assert [artifact.offset_seconds for artifact in generation.artifacts] == [0, 10, 20]
    assert container.seek_calls == [10, 20]
    assert container.decode_calls == 3
    assert all((workspace / artifact.relative_path).read_bytes().startswith(b"RIFF") for artifact in generation.artifacts)


def test_create_clip_uses_single_media_ffmpeg_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider, library, media_root, _import_root = _provider(tmp_path)
    source = media_root / "videos/clip.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"media")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        Path(command[-1]).write_bytes(b"mp4")

    monkeypatch.setattr(storage_module.subprocess, "run", fake_run)
    artifact = provider.create_clip(
        media=_media(library, "videos/clip.mp4"),
        start_offset_seconds=1,
        end_offset_seconds=4,
        workspace=tmp_path / "clips",
    )
    assert artifact.relative_path == "clip.mp4"
    assert (tmp_path / "clips/clip.mp4").read_bytes() == b"mp4"
    assert calls and calls[0][0] == "ffmpeg" and "-c" in calls[0]


def test_import_receipt_is_opaque_and_operation_key_conflicts_are_rejected(tmp_path: Path) -> None:
    provider, _library, _media_root, import_root = _provider(tmp_path)
    source_path = import_root / "clip.mp4"
    source_path.write_bytes(b"source")
    source = provider.scan_import_source(
        source_ref={"version": 1, "kind": "manual_local_path", "relative_path": "clip.mp4"}
    )[0]
    staged = provider.stage_import_file(
        source=source,
        placement=ImportPlacement(relative_path="videos/clip.mp4"),
        source_disposition="keep",
        operation_key="same-key",
    )
    assert set(staged.receipt) == {"operation_key", "token"}
    with pytest.raises(ProviderOperationError):
        provider.finalize_import(
            receipt={
                "operation_key": staged.receipt["operation_key"],
                "token": staged.receipt["token"],
                "relative_path": "videos/other.mp4",
            }
        )
    with pytest.raises(ProviderOperationError) as error:
        provider.stage_import_file(
            source=source,
            placement=ImportPlacement(relative_path="videos/other.mp4"),
            source_disposition="keep",
            operation_key="same-key",
        )
    assert error.value.code == "invalid_config"


def test_finalize_does_not_delete_replaced_source_and_missing_target_is_structured(
    tmp_path: Path,
) -> None:
    provider, _library, media_root, import_root = _provider(tmp_path)
    source_path = import_root / "clip.mp4"
    source_path.write_bytes(b"source")
    source = provider.scan_import_source(
        source_ref={"version": 1, "kind": "manual_local_path", "relative_path": "clip.mp4"}
    )[0]
    staged = provider.stage_import_file(
        source=source,
        placement=ImportPlacement(relative_path="videos/clip.mp4"),
        source_disposition="delete_after_commit",
        operation_key="replace-source",
    )
    replacement = import_root / "replacement.mp4"
    replacement.write_bytes(b"new source")
    replacement.replace(source_path)
    with pytest.raises(ProviderOperationError) as error:
        provider.finalize_import(receipt=staged.receipt)
    assert error.value.code == "source_not_found"
    assert source_path.exists()
    assert source_path.read_bytes() == b"new source"

    target = media_root / "videos/replace-source/clip.mp4"
    target.unlink()
    with pytest.raises(ProviderOperationError) as error:
        provider.finalize_import(receipt=staged.receipt)
    assert error.value.code == "source_not_found"


def test_stage_cleans_partial_copy_and_abort_is_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _library, media_root, import_root = _provider(tmp_path)
    source_path = import_root / "clip.mp4"
    source_path.write_bytes(b"source")
    source = provider.scan_import_source(
        source_ref={"version": 1, "kind": "manual_local_path", "relative_path": "clip.mp4"}
    )[0]

    def interrupted_copy(_source: Path, destination: Path) -> None:
        destination.write_bytes(b"partial")
        raise OSError("interrupted")

    monkeypatch.setattr(storage_module.shutil, "copy2", interrupted_copy)
    with pytest.raises(ProviderOperationError):
        provider.stage_import_file(
            source=source,
            placement=ImportPlacement(relative_path="videos/clip.mp4"),
            source_disposition="delete_after_commit",
            operation_key="partial-copy",
        )
    target_dir = media_root / "videos/partial-copy"
    assert not (target_dir / "clip.mp4").exists()
    assert not list(target_dir.glob(".staging-*"))

    monkeypatch.undo()
    staged = provider.stage_import_file(
        source=source,
        placement=ImportPlacement(relative_path="videos/clip.mp4"),
        source_disposition="keep",
        operation_key="abort-retry",
    )
    provider.abort_import(receipt=staged.receipt)
    provider.abort_import(receipt=staged.receipt)
    assert not (media_root / "videos/abort-retry/clip.mp4").exists()


def test_playback_head_and_invalid_range_have_no_body_or_real_size(tmp_path: Path) -> None:
    provider, library, media_root, _import_root = _provider(tmp_path)
    path = media_root / "videos/clip.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"0123456789")
    media = _media(library, "videos/clip.mp4")
    head = Request(
        {
            "type": "http",
            "method": "HEAD",
            "path": "/",
            "headers": [],
        }
    )
    response = asyncio.run(
        provider.handle_playback(
            media=media,
            context=PlaybackContext(
                request=head,
                resource_path="",
                delivery="proxy",
                url_for=lambda value: value,
            ),
        )
    )
    assert response.status_code == 200
    assert response.body == b""
    assert response.headers["content-length"] == "10"

    invalid = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"range", b"bytes=99-")],
        }
    )
    response = asyncio.run(
        provider.handle_playback(
            media=media,
            context=PlaybackContext(
                request=invalid,
                resource_path="",
                delivery="proxy",
                url_for=lambda value: value,
            ),
        )
    )
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"


def test_workspace_symlink_is_rejected_and_clip_timeout_cleans_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, library, media_root, _import_root = _provider(tmp_path)
    source = media_root / "videos/clip.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"media")
    media = _media(library, "videos/clip.mp4")
    real_workspace = tmp_path / "real-workspace"
    real_workspace.mkdir()
    symlink_workspace = tmp_path / "symlink-workspace"
    try:
        symlink_workspace.symlink_to(real_workspace, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ProviderOperationError):
        provider.create_clip(
            media=media,
            start_offset_seconds=0,
            end_offset_seconds=1,
            workspace=symlink_workspace,
        )

    def timeout(*_args, **_kwargs):
        raise storage_module.subprocess.TimeoutExpired("ffmpeg", 300)

    monkeypatch.setattr(storage_module.subprocess, "run", timeout)
    with pytest.raises(ProviderOperationError) as error:
        provider.create_clip(
            media=media,
            start_offset_seconds=0,
            end_offset_seconds=1,
            workspace=real_workspace,
        )
    assert error.value.code == "unavailable"
    assert error.value.retryable
    assert not list(real_workspace.glob(".clip-*"))


def test_target_same_length_in_place_rewrite_is_rejected(tmp_path: Path) -> None:
    provider, _library, media_root, import_root = _provider(tmp_path)
    source_path = import_root / "clip.mp4"
    source_path.write_bytes(b"source")
    source = provider.scan_import_source(
        source_ref={"version": 1, "kind": "manual_local_path", "relative_path": "clip.mp4"}
    )[0]
    staged = provider.stage_import_file(
        source=source,
        placement=ImportPlacement(relative_path="videos/clip.mp4"),
        source_disposition="delete_after_commit",
        operation_key="target-mtime",
    )
    target = media_root / "videos/target-mtime/clip.mp4"
    original_mtime = target.stat().st_mtime_ns
    target.write_bytes(b"changed")
    if target.stat().st_mtime_ns == original_mtime:
        os.utime(target, ns=(original_mtime, original_mtime + 1))
    with pytest.raises(ProviderOperationError) as error:
        provider.finalize_import(receipt=staged.receipt)
    assert error.value.code == "source_not_found"
    assert source_path.exists()


def test_finalized_operation_is_idempotent_even_if_target_is_missing(tmp_path: Path) -> None:
    provider, _library, media_root, import_root = _provider(tmp_path)
    source_path = import_root / "clip.mp4"
    source_path.write_bytes(b"source")
    source = provider.scan_import_source(
        source_ref={"version": 1, "kind": "manual_local_path", "relative_path": "clip.mp4"}
    )[0]
    staged = provider.stage_import_file(
        source=source,
        placement=ImportPlacement(relative_path="videos/clip.mp4"),
        source_disposition="keep",
        operation_key="finalized-idempotent",
    )
    provider.finalize_import(receipt=staged.receipt)
    (media_root / "videos/finalized-idempotent/clip.mp4").unlink()
    provider.finalize_import(receipt=staged.receipt)


def test_stage_rejects_invalid_operation_key_and_placement_structured(tmp_path: Path) -> None:
    provider, _library, _media_root, import_root = _provider(tmp_path)
    source_path = import_root / "clip.mp4"
    source_path.write_bytes(b"source")
    source = provider.scan_import_source(
        source_ref={"version": 1, "kind": "manual_local_path", "relative_path": "clip.mp4"}
    )[0]
    with pytest.raises(ProviderOperationError) as operation_error:
        provider.stage_import_file(
            source=source,
            placement=ImportPlacement(relative_path="videos/clip.mp4"),
            source_disposition="keep",
            operation_key="../escape",
        )
    assert operation_error.value.code == "invalid_config"
    with pytest.raises(ProviderOperationError) as placement_error:
        provider.stage_import_file(
            source=source,
            placement=ImportPlacement(relative_path="invalid"),
            source_disposition="keep",
            operation_key="valid-operation",
        )
    assert placement_error.value.code == "invalid_config"


def test_preparing_journal_recovers_target_after_interrupted_final_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _library, media_root, import_root = _provider(tmp_path)
    source_path = import_root / "clip.mp4"
    source_path.write_bytes(b"source")
    source = provider.scan_import_source(
        source_ref={"version": 1, "kind": "manual_local_path", "relative_path": "clip.mp4"}
    )[0]
    original_write = provider._write_journal

    def fail_final_write(journal, *, operation):
        if journal["state"] == "staged":
            raise KeyboardInterrupt
        return original_write(journal, operation=operation)

    monkeypatch.setattr(provider, "_write_journal", fail_final_write)
    with pytest.raises(KeyboardInterrupt):
        provider.stage_import_file(
            source=source,
            placement=ImportPlacement(relative_path="videos/clip.mp4"),
            source_disposition="keep",
            operation_key="interrupted-final-write",
        )
    target = media_root / "videos/interrupted-final-write/clip.mp4"
    assert target.exists()

    monkeypatch.setattr(provider, "_write_journal", original_write)
    staged = provider.stage_import_file(
        source=source,
        placement=ImportPlacement(relative_path="videos/clip.mp4"),
        source_disposition="keep",
        operation_key="interrupted-final-write",
    )
    assert staged.size_bytes == len(b"source")
    assert target.read_bytes() == b"source"
    assert not list(target.parent.glob(".staging-*"))


def test_scan_import_sort_is_casefold_then_original_name(tmp_path: Path) -> None:
    provider, _library, _media_root, import_root = _provider(tmp_path)
    (import_root / "a.mp4").write_bytes(b"a")
    (import_root / "A.mp4").write_bytes(b"A")
    if not (import_root / "A.mp4").is_file() or (import_root / "A.mp4").samefile(import_root / "a.mp4"):
        pytest.skip("filesystem is case-insensitive")
    files = provider.scan_import_source(
        source_ref={"version": 1, "kind": "manual_local_path", "relative_path": ""}
    )
    assert [item.relative_path for item in files] == ["A.mp4", "a.mp4"]


@pytest.mark.parametrize("suffix", [".mts", ".f4v", ".rm", ".rmvb", ".3gp", ".ogv"])
def test_scan_recognises_video_suffixes(tmp_path: Path, suffix: str) -> None:
    provider, _library, _media_root, import_root = _provider(tmp_path)
    path = import_root / f"clip{suffix}"
    path.write_bytes(b"video")
    files = provider.scan_import_source(
        source_ref={"version": 1, "kind": "manual_local_path", "relative_path": ""}
    )
    assert len(files) == 1
    assert files[0].is_video is True


@pytest.mark.parametrize("change", ["none", "replace", "remove", "symlink", "close", "wrong_media", "wrong_owner"])
def test_transfer_cleanup_only_removes_the_same_open_source(tmp_path, change):
    from dataclasses import replace
    provider, library, root, _ = _provider(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "video.mp4"
    path.write_bytes(b"test-bytes")
    media = _media(library, "video.mp4")
    media = replace(media, file_size_bytes=10)
    download = tmp_path / "download.mp4"
    import os
    os.link(path, download)
    with provider.open_transfer_source(media=media) as source:
        cleanup_media, cleanup_provider = media, provider
        if change == "replace":
            path.unlink()
            path.write_bytes(b"replacement")
        elif change == "remove":
            path.unlink()
        elif change == "symlink":
            path.unlink()
            path.symlink_to(download)
        elif change == "close":
            source._close()
        elif change == "wrong_media":
            cleanup_media = replace(media, media_id=media.media_id + 1)
        elif change == "wrong_owner":
            cleanup_provider = LocalStorageProvider(library=library, data_dir=tmp_path / "other-data")
        if change == "none":
            cleanup_provider.cleanup_transfer_source(media=cleanup_media, source=source)
            assert not path.exists()
        else:
            with pytest.raises(ProviderOperationError):
                cleanup_provider.cleanup_transfer_source(media=cleanup_media, source=source)
            if change != "remove":
                assert path.exists()
    assert download.read_bytes() == b"test-bytes"
    assert root.is_dir()
