#!/usr/bin/env python3
"""Validate a per-plugin GitHub Release tag and emit build metadata."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

TAG_RE = re.compile(
    r"^(?P<plugin_id>[a-z][a-z0-9_]*)-v(?P<version>\d+\.\d+\.\d+)$"
)
PAGES_ROOT = "https://tinypinglite.github.io/sakuramedia-plugin-market/releases"
# 仓库根目录允许存在的非插件文件；其余未知条目视为目录结构错误。
REPO_FILES = {
    ".gitignore",
    "LICENSE",
    "README.md",
    "README.zh-CN.md",
    "CONTRIBUTING.md",
}


def fail(message: str) -> None:
    raise SystemExit(f"release validation failed: {message}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--github-output", default=None)
    parser.add_argument("--prerelease", default="false")
    args = parser.parse_args()

    match = TAG_RE.fullmatch(args.tag)
    if match is None:
        fail(f"tag must be <plugin_id>-v<version>: {args.tag!r}")
    if args.prerelease == "true":
        fail("prerelease builds are not enabled in this iteration")

    plugin_id = match.group("plugin_id")
    version = match.group("version")
    root = Path(args.repo_root).resolve()
    plugin_dir = root / plugin_id
    manifest_path = plugin_dir / "manifest.json"
    if not manifest_path.is_file():
        fail(f"missing {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("plugin_id") != plugin_id:
        fail("manifest plugin_id does not match directory/tag")
    if manifest.get("version") != version:
        fail(
            f"manifest version {manifest.get('version')!r} != tag version {version!r}"
        )
    if manifest.get("host_api_version") != 18:
        fail("host_api_version must be 18")
    if manifest.get("requires_python") != ">=3.10,<3.11":
        fail("requires_python must be >=3.10,<3.11")

    expected_api_url = f"{PAGES_ROOT}/{plugin_id}.json"
    if manifest.get("release_api_url") != expected_api_url:
        fail(f"release_api_url must be {expected_api_url}")

    plugin_dirs = {
        path.name
        for path in root.iterdir()
        if path.is_dir() and (path / "manifest.json").is_file()
    }
    unexpected = [
        path.name
        for path in root.iterdir()
        if path.name not in {".github", ".git"}
        and path.name not in REPO_FILES
        and path.name not in plugin_dirs
    ]
    if unexpected:
        fail(f"unexpected top-level entries: {unexpected}")

    outputs = {
        "plugin_id": plugin_id,
        "version": version,
        "tag": args.tag,
        "display_name": str(manifest.get("display_name", plugin_id)),
        "host_api_version": "18",
    }
    for key, value in outputs.items():
        print(f"{key}={value}")
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            for key, value in outputs.items():
                handle.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
