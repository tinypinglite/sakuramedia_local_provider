#!/usr/bin/env python3
"""Assemble a compiled plugin zip: shim + dual-arch .so + manifest/assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

MAX_ZIP_BYTES = 100 * 1024 * 1024
MAX_UNPACKED_BYTES = 500 * 1024 * 1024
MAX_FILES = 5000


def fail(message: str) -> None:
    raise SystemExit(f"package failed: {message}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-dir", required=True)
    parser.add_argument("--so-dir", required=True)
    parser.add_argument("--shim", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--release-api-url", required=True)
    args = parser.parse_args()

    plugin_dir = Path(args.plugin_dir)
    manifest_path = plugin_dir / "manifest.json"
    if not manifest_path.is_file():
        fail(f"missing {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plugin_id = manifest["plugin_id"]
    version = manifest["version"]
    manifest["release_api_url"] = args.release_api_url

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    package_dir = output_dir / plugin_id
    shutil.rmtree(package_dir, ignore_errors=True)
    package_dir.mkdir(parents=True)

    shutil.copy2(Path(args.shim), package_dir / "__init__.py")
    (package_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    so_files = sorted(Path(args.so_dir).rglob("*.so"))
    if len(so_files) != 2:
        fail(f"expected exactly two .so files, got {[p.name for p in so_files]}")
    names = [path.name for path in so_files]
    if not any("x86_64" in name for name in names):
        fail(f"missing amd64 .so in {names}")
    if not any("aarch64" in name for name in names):
        fail(f"missing arm64 .so in {names}")
    for path in so_files:
        shutil.copy2(path, package_dir / path.name)

    for directory in ("i18n", "prompts"):
        source = plugin_dir / directory
        if source.is_dir():
            shutil.copytree(source, package_dir / directory)
    for filename in ("top250.json", "LICENSE", "README.md"):
        source = plugin_dir / filename
        if source.is_file():
            shutil.copy2(source, package_dir / filename)

    py_files = sorted(
        path.relative_to(package_dir).as_posix()
        for path in package_dir.rglob("*.py")
    )
    if py_files != ["__init__.py"]:
        fail(f"unexpected .py files in package: {py_files}")

    files = [path for path in package_dir.rglob("*") if path.is_file()]
    if len(files) > MAX_FILES:
        fail(f"too many files: {len(files)}")
    unpacked = sum(path.stat().st_size for path in files)
    if unpacked > MAX_UNPACKED_BYTES:
        fail(f"unpacked size too large: {unpacked}")

    zip_path = output_dir / f"{plugin_id}-{version}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, path.relative_to(package_dir).as_posix())
    zip_size = zip_path.stat().st_size
    if zip_size > MAX_ZIP_BYTES:
        fail(f"zip size too large: {zip_size}")

    digest = sha256_file(zip_path)
    (output_dir / f"{zip_path.name}.sha256").write_text(
        f"{digest}  {zip_path.name}\n", encoding="utf-8"
    )
    print(f"zip={zip_path}")
    print(f"sha256={digest}")
    print(f"size={zip_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
