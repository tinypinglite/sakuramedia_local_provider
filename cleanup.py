"""Manual, sequential cleanup of empty local media directories."""

from __future__ import annotations

import errno
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass
class _Directory:
    path: Path
    children: list[_Directory] = field(default_factory=list)
    blocked: bool = False


class _Progress:
    def __init__(self, reporter):
        self.reporter = reporter
        self.stats = {
            "scanned": 0,
            "scanned_entries": 0,
            "processed": 0,
            "succeeded": 0,
            "skipped": 0,
            "failed": 0,
            "scan_errors": 0,
            "pending": 0,
        }
        self.stage = 1
        self.location = ""
        self.total = 0
        self.last_report = self.last_log = float("-inf")

    def report(self, action, *, force=False):
        now = time.monotonic()
        report = force or now - self.last_report >= 2
        log = force or now - self.last_log >= 10
        if not report and not log:
            return
        s = self.stats
        current = s["scanned"] if self.stage == 1 else s["processed"]
        stage = "扫描目录" if self.stage == 1 else "清理空目录"
        counts = (
            f"已检查 {current} 个目录/{s['scanned_entries']} 个条目 · 待处理总量未知"
            if self.stage == 1
            else f"已处理 {current}/{self.total} · 待处理 {s['pending']}"
        )
        text = (
            f"阶段 {self.stage}/2 · {stage} · {self.location}{action} · {counts} · "
            f"删除 {s['succeeded']} · 跳过 {s['skipped']} · 失败 {s['failed']} · "
            f"扫描错误 {s['scan_errors']}"
        )
        if report:
            summary = dict(s)
            if self.stage == 1:
                summary.pop("pending")
            if self.reporter is not None:
                self.reporter.progress_callback(
                    {
                        "current": current,
                        "total": self.total,
                        "text": text[:255],
                        "summary_patch": summary,
                    }
                )
            self.last_report = now
        if log:
            logger.info("本地空目录清理 {}", text)
            self.last_log = now

    def scan_error(self, path, exc):
        self.stats["scan_errors"] += 1
        logger.warning("本地空目录扫描失败 path={} error={}", path, exc)

    def finish(self, outcome):
        self.stats["processed"] += 1
        self.stats[outcome] += 1
        self.stats["pending"] -= 1
        self.report("正在处理目录")


def _open_root(path):
    # Anchor every component without following symlinks, including configured ancestors.
    fd = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _scan(root, progress, protected):
    fd = _open_root(root.path)
    frames = [(root, fd, None)]
    try:
        while frames:
            node, fd, children = frames[-1]
            if children is None:
                try:
                    progress.report("正在读取目录条目")
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            progress.stats["scanned_entries"] += 1
                            progress.report("正在读取目录条目")
                            path = node.path / entry.name
                            if path not in protected and entry.is_dir(
                                follow_symlinks=False
                            ):
                                node.children.append(_Directory(path))
                            else:
                                node.blocked = True
                    progress.stats["scanned"] += 1
                except OSError as exc:
                    node.blocked = True
                    progress.scan_error(node.path, exc)
                children = iter(node.children)
                frames[-1] = (node, fd, children)
            child = next(children, None)
            if child is None:
                os.close(fd)
                frames.pop()
                continue
            try:
                progress.report("正在打开子目录")
                child_fd = os.open(child.path.name, _DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                child.blocked = True
            except OSError as exc:
                child.blocked = True
                progress.scan_error(child.path, exc)
            else:
                frames.append((child, child_fd, None))
    finally:
        for _, fd, _ in frames:
            os.close(fd)


def _count_children(root):
    count = 0
    stack = list(root.children)
    while stack:
        node = stack.pop()
        count += 1
        stack.extend(node.children)
    return count


def _clean(root, progress):
    if not root.children:
        return
    fd = _open_root(root.path)
    frames = [(root, fd, iter(root.children))]
    try:
        while frames:
            node, parent_fd, children = frames[-1]
            child = next(children, None)
            if child is not None:
                if child.blocked and not child.children:
                    progress.finish("skipped")
                    node.blocked = True
                    continue
                try:
                    progress.report("正在打开待清理目录")
                    child_fd = os.open(
                        child.path.name, _DIRECTORY_FLAGS, dir_fd=parent_fd
                    )
                except OSError as exc:
                    # Descendants cannot be reached safely after this directory changed.
                    outcome = (
                        "skipped"
                        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP)
                        else "failed"
                    )
                    logger.warning(
                        "本地空目录清理无法访问 path={} error={}", child.path, exc
                    )
                    for _ in range(1 + _count_children(child)):
                        progress.finish(outcome)
                    node.blocked = True
                else:
                    frames.append((child, child_fd, iter(child.children)))
                continue
            os.close(parent_fd)
            frames.pop()
            if not frames:
                break  # The jav/videos root itself is never removed.
            parent, parent_fd, _ = frames[-1]
            outcome = "skipped"
            if not node.blocked:
                try:
                    os.rmdir(node.path.name, dir_fd=parent_fd)
                    outcome = "succeeded"
                except OSError as exc:
                    if exc.errno not in (
                        errno.ENOTEMPTY,
                        errno.EEXIST,
                        errno.ENOENT,
                        errno.ENOTDIR,
                        errno.ELOOP,
                    ):
                        outcome = "failed"
                        logger.warning(
                            "本地空目录删除失败 path={} error={}", node.path, exc
                        )
            if outcome != "succeeded":
                parent.blocked = True
            progress.finish(outcome)
    finally:
        for _, fd, _ in frames:
            os.close(fd)


def _load_media_libraries():
    from src.model import MediaLibrary

    return tuple(
        MediaLibrary.select()
        .where(MediaLibrary.provider_key == "local")
        .order_by(MediaLibrary.id.asc())
    )


def cleanup_empty_media_dirs(reporter, params):
    progress = _Progress(reporter)
    progress.report("开始读取媒体库", force=True)
    roots = []
    media_roots = set()
    for library in _load_media_libraries():
        try:
            value = library.provider_config["media_root_path"]
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError("invalid media_root_path")
            media_root = Path(os.path.abspath(Path(value).expanduser()))
        except (KeyError, TypeError, ValueError) as exc:
            progress.scan_error(f"媒体库 {library.id}", exc)
            continue
        media_roots.add(media_root)
    protected = media_roots | {
        root / name for root in media_roots for name in ("jav", "videos")
    }
    for index, media_root in enumerate(sorted(media_roots), 1):
        for name in ("jav", "videos"):
            path = media_root / name
            progress.location = f"媒体库 {index}/{len(media_roots)} · {name} · "
            root = _Directory(path)
            try:
                _scan(root, progress, protected)
            except FileNotFoundError:
                continue
            except OSError as exc:
                progress.scan_error(path, exc)
                continue
            roots.append(root)
    progress.location = ""
    progress.total = progress.stats["scanned"]
    progress.report("扫描结束", force=True)
    progress.stage = 2
    progress.total = sum(_count_children(root) for root in roots)
    progress.stats["pending"] = progress.total
    progress.report("开始清理", force=True)
    for index, root in enumerate(roots, 1):
        progress.location = f"目录入口 {index}/{len(roots)} · {root.path.name} · "
        try:
            _clean(root, progress)
        except OSError as exc:
            logger.warning("本地空目录清理入口不可用 path={} error={}", root.path, exc)
            outcome = (
                "skipped"
                if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP)
                else "failed"
            )
            for _ in range(_count_children(root)):
                progress.finish(outcome)
    progress.location = ""
    progress.report(
        "任务完成；扫描错误范围未完成检查"
        if progress.stats["scan_errors"]
        else "任务完成",
        force=True,
    )
    return dict(progress.stats)
