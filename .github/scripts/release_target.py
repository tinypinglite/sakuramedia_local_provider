#!/usr/bin/env python3
"""Validate a release tag for the standalone sakuramedia_local_provider repo."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

PLUGIN_ID = "sakuramedia_local_provider"
TAG_RE = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
RELEASE_API_URL = (
    "https://api.github.com/repos/tinypinglite/"
    "sakuramedia_local_provider/releases/latest"
)


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
        fail(f"tag must be v<version>: {args.tag!r}")
    if args.prerelease == "true":
        fail("prerelease builds are not enabled in this iteration")

    version = match.group("version")
    root = Path(args.repo_root).resolve()
    plugin_dir = root / PLUGIN_ID
    manifest_path = plugin_dir / "manifest.json"
    if not manifest_path.is_file():
        fail(f"missing {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("plugin_id") != PLUGIN_ID:
        fail("manifest plugin_id does not match repo")
    if manifest.get("version") != version:
        fail(
            f"manifest version {manifest.get('version')!r} != tag version {version!r}"
        )
    if manifest.get("host_api_version") != 18:
        fail("host_api_version must be 18")
    if manifest.get("requires_python") != ">=3.10,<3.11":
        fail("requires_python must be >=3.10,<3.11")
    if manifest.get("release_api_url") != RELEASE_API_URL:
        fail(f"release_api_url must be {RELEASE_API_URL}")

    outputs = {
        "plugin_id": PLUGIN_ID,
        "version": version,
        "tag": args.tag,
        "display_name": str(manifest.get("display_name", PLUGIN_ID)),
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
