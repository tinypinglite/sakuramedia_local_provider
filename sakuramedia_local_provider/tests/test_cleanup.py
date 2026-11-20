from __future__ import annotations

import errno
import os
from collections import Counter
from itertools import pairwise
from types import SimpleNamespace

import pytest
from sakuramedia_local_provider import cleanup


class Reporter:
    def __init__(self, hook=None):
        self.updates = []
        self.hook = hook

    def progress_callback(self, payload):
        self.updates.append(payload)
        if self.hook:
            self.hook(payload)


@pytest.fixture
def run_cleanup(monkeypatch):
    def run(*roots, reporter=None):
        libraries = [
            SimpleNamespace(id=index, provider_config={"media_root_path": str(root)})
            for index, root in enumerate(roots)
        ]
        monkeypatch.setattr(cleanup, "_load_media_libraries", lambda: libraries)
        return cleanup.cleanup_empty_media_dirs(reporter, {})

    return run


def test_recursive_cleanup_preserves_roots_files_and_links(tmp_path, run_cleanup):
    root = tmp_path / "media"
    (root / "jav/a/b/c").mkdir(parents=True)
    (root / "videos/empty").mkdir(parents=True)
    (root / "other/empty").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "empty").mkdir(parents=True)
    (root / "jav/link").symlink_to(outside, target_is_directory=True)
    (root / "jav/broken").symlink_to(tmp_path / "missing")
    for name in (".hidden", "subtitle.srt", "poster.jpg", "zero.mp4"):
        folder = root / "videos" / name
        folder.mkdir()
        (folder / name).touch()
    result = run_cleanup(root, root, tmp_path / "absent")
    assert result["succeeded"] == 4
    assert result["skipped"] == 4
    assert result["failed"] == result["scan_errors"] == result["pending"] == 0
    assert not (root / "jav/a").exists()
    assert (root / "jav").is_dir() and (root / "videos").is_dir()
    assert (root / "other/empty").is_dir()
    assert (outside / "empty").is_dir()
    assert (root / "jav/broken").is_symlink()
    assert not (tmp_path / "absent").exists()
    assert all(
        (root / "videos" / name / name).is_file()
        for name in (".hidden", "subtitle.srt", "poster.jpg", "zero.mp4")
    )


def test_nested_library_roots_are_protected(tmp_path, run_cleanup):
    inner = tmp_path / "jav/nested"
    (inner / "jav/empty").mkdir(parents=True)
    (inner / "videos/empty").mkdir(parents=True)
    run_cleanup(tmp_path, inner)
    assert inner.is_dir()
    assert (inner / "jav").is_dir() and (inner / "videos").is_dir()
    assert not (inner / "jav/empty").exists()


def test_scan_enumerates_each_directory_once_and_never_reads_files(
    tmp_path, monkeypatch, run_cleanup
):
    for path in ("jav/a/b", "jav/full", "videos/c"):
        (tmp_path / path).mkdir(parents=True)
    (tmp_path / "jav/full/media.mp4").touch()
    calls = Counter()
    original = os.scandir
    original_open = os.open

    def open_directory(path, flags, **kwargs):
        assert flags & os.O_DIRECTORY
        return original_open(path, flags, **kwargs)

    def scandir(fd):
        calls[os.fstat(fd).st_ino] += 1
        return original(fd)

    monkeypatch.setattr(cleanup.os, "scandir", scandir)
    monkeypatch.setattr(cleanup.os, "open", open_directory)
    result = run_cleanup(tmp_path)
    assert len(calls) == 6
    assert set(calls.values()) == {1}
    assert result["succeeded"] == 3
    assert result["skipped"] == 1


def test_file_appearing_before_delete_is_preserved(tmp_path, run_cleanup):
    target = tmp_path / "jav/a/b"
    target.mkdir(parents=True)

    def change(payload):
        if "开始清理" in payload["text"]:
            (target / "new.mp4").touch()

    result = run_cleanup(tmp_path, reporter=Reporter(change))
    assert (target / "new.mp4").exists()
    assert result["succeeded"] == 0
    assert result["skipped"] == 2


def test_directory_replaced_with_symlink_between_stages_is_not_followed(
    tmp_path, run_cleanup
):
    root = tmp_path / "media"
    target = root / "jav/parent/empty"
    target.mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "empty").mkdir(parents=True)

    def change(payload):
        if "开始清理" in payload["text"]:
            target.rmdir()
            target.parent.rmdir()
            target.parent.symlink_to(outside, target_is_directory=True)

    result = run_cleanup(root, reporter=Reporter(change))
    assert (outside / "empty").is_dir()
    assert target.parent.is_symlink()
    assert result["skipped"] == 2
    assert result["pending"] == 0


def test_missing_directory_before_cleanup_is_skipped(tmp_path, run_cleanup):
    target = tmp_path / "jav/empty"
    target.mkdir(parents=True)

    def change(payload):
        if "开始清理" in payload["text"]:
            target.rmdir()

    result = run_cleanup(tmp_path, reporter=Reporter(change))
    assert result["skipped"] == 1
    assert result["failed"] == result["pending"] == 0


def test_scan_and_delete_errors_continue_other_libraries(
    tmp_path, monkeypatch, run_cleanup
):
    root = tmp_path / "first"
    (root / "jav/unreadable").mkdir(parents=True)
    (root / "videos/undeletable").mkdir(parents=True)
    second = tmp_path / "second"
    (second / "jav/empty").mkdir(parents=True)
    unreadable_ino = (root / "jav/unreadable").stat().st_ino
    scandir = os.scandir
    rmdir = os.rmdir

    def fail_scan(fd):
        if os.fstat(fd).st_ino == unreadable_ino:
            raise PermissionError(errno.EACCES, "scan denied")
        return scandir(fd)

    def fail_delete(path, *, dir_fd=None):
        if path == "undeletable":
            raise PermissionError(errno.EACCES, "delete denied")
        return rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(cleanup.os, "scandir", fail_scan)
    monkeypatch.setattr(cleanup.os, "rmdir", fail_delete)
    reporter = Reporter()
    result = run_cleanup(root, second, reporter=reporter)
    assert result["scan_errors"] == result["failed"] == result["succeeded"] == 1
    assert result["pending"] == 0
    assert (root / "jav/unreadable").is_dir()
    assert (root / "videos/undeletable").is_dir()
    assert not (second / "jav/empty").exists()
    assert "扫描错误范围未完成检查" in reporter.updates[-1]["text"]


def test_configured_symlink_ancestor_is_rejected(tmp_path, run_cleanup):
    real = tmp_path / "real"
    (real / "media/jav/empty").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    result = run_cleanup(link / "media")
    assert result["scan_errors"] == 2
    assert (real / "media/jav/empty").is_dir()


def test_invalid_configuration_and_empty_library_list(monkeypatch):
    libraries = [
        SimpleNamespace(id=1, provider_config={}),
        SimpleNamespace(id=2, provider_config=None),
    ]
    monkeypatch.setattr(cleanup, "_load_media_libraries", lambda: libraries)
    reporter = Reporter()
    result = cleanup.cleanup_empty_media_dirs(reporter, {})
    assert result["scan_errors"] == 2
    assert result["pending"] == 0
    libraries.clear()
    reporter = Reporter()
    result = cleanup.cleanup_empty_media_dirs(reporter, {})
    assert len(reporter.updates) == 4
    assert not any(result.values())


def test_progress_stages_cumulative_summary_and_slow_large_directory(
    tmp_path, monkeypatch, run_cleanup
):
    folder = tmp_path / "jav/full"
    folder.mkdir(parents=True)
    for i in range(30):
        (folder / str(i)).touch()
    for i in range(12):
        (tmp_path / "jav" / f"empty{i}").mkdir()
    clock = [0.0]
    events = []
    logs = []
    original_scandir = os.scandir
    original_rmdir = os.rmdir

    class SlowScan:
        def __init__(self, fd):
            self.entries = original_scandir(fd)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.entries.close()

        def __iter__(self):
            return self

        def __next__(self):
            value = next(self.entries)
            clock[0] += 0.6
            return value

    def slow_delete(path, *, dir_fd=None):
        clock[0] += 0.8
        return original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(cleanup.os, "scandir", SlowScan)
    monkeypatch.setattr(cleanup.os, "rmdir", slow_delete)
    monkeypatch.setattr(cleanup.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cleanup.logger, "info", lambda *args: logs.append(clock[0]))
    reporter = Reporter(lambda payload: events.append(clock[0]))
    result = run_cleanup(tmp_path, reporter=reporter)
    updates = reporter.updates
    for label in ("扫描目录", "清理空目录"):
        stage = [u for u in updates if label in u["text"]]
        assert stage[0]["current"] == 0
        assert stage[-1]["current"] == stage[-1]["total"]
        assert [u["current"] for u in stage] == sorted(u["current"] for u in stage)
    scanning = [u for u in updates if "扫描目录" in u["text"]]
    assert all(u["total"] == 0 for u in scanning[:-1])
    assert all("pending" not in u["summary_patch"] for u in scanning)
    assert (
        len(scanning) > 5
    )  # File-entry iteration reports even within one large directory.
    for key in (
        "scanned",
        "scanned_entries",
        "processed",
        "succeeded",
        "skipped",
        "failed",
        "scan_errors",
    ):
        values = [u["summary_patch"][key] for u in updates]
        assert values == sorted(values)
    assert max(b - a for a, b in pairwise(events)) < 3
    assert max(b - a for a, b in pairwise(logs)) < 11
    assert all(len(u["text"]) <= 255 for u in updates)
    assert all("删除" in u["text"] and "失败" in u["text"] for u in updates)
    assert updates[-1]["summary_patch"] == result
    assert "待处理 0" in updates[-1]["text"]
    assert result["succeeded"] == 12
