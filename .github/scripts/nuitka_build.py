#!/usr/bin/env python3
"""Compile one plugin package with Nuitka inside the backend build image."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

NOFOLLOW_IMPORTS = (
    "src",
    "pydantic",
    "fastapi",
    "starlette",
    "httpx",
    "loguru",
    "typing_extensions",
    "av",
    "PIL",
    "qbittorrentapi",
    "libtorrent",
    "bs4",
    "soupsieve",
    "curl_cffi",
    "portalocker",
)


def run(cmd: list[str], *, cwd: Path) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-id", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    root = Path.cwd()
    plugin_dir = root / args.plugin_id
    if not (plugin_dir / "manifest.json").is_file():
        raise SystemExit(f"plugin dir missing: {plugin_dir}")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    build_dir = root / ".nuitka-build" / args.arch / args.plugin_id
    shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--module",
        args.plugin_id,
        f"--include-package={args.plugin_id}",
        "--file-reference-choice=runtime",
        "--remove-output",
        "--assume-yes-for-downloads",
        f"--output-dir={build_dir}",
    ]
    for name in NOFOLLOW_IMPORTS:
        cmd.append(f"--nofollow-import-to={name}")
    run(cmd, cwd=root)

    suffix = sysconfig.get_config_var("EXT_SUFFIX") or ""
    so_files = sorted(build_dir.glob("*.so"))
    if len(so_files) != 1:
        raise SystemExit(f"expected one .so in {build_dir}, got {so_files}")
    target = out_dir / f"{args.plugin_id}{suffix}"
    shutil.copy2(so_files[0], target)
    subprocess.run(["strip", "--strip-debug", str(target)], check=True)

    file_output = subprocess.check_output(["file", str(target)], text=True)
    print(file_output.strip())
    nm_output = subprocess.check_output(["nm", "-D", str(target)], text=True)
    if f"PyInit_{args.plugin_id}" not in nm_output:
        raise SystemExit(f"PyInit_{args.plugin_id} not found in {target}")
    print(f"built {target} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
