from __future__ import annotations

import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = PLUGIN_ROOT.parent
host_root = os.environ.get("SAKURAMEDIA_HOST_ROOT")
if not host_root:
    raise RuntimeError(
        "测试需要宿主仓库，请设置 SAKURAMEDIA_HOST_ROOT 指向 SakuraMediaBE 根目录"
    )
HOST_ROOT = Path(host_root).expanduser()
if not (HOST_ROOT / "src/plugins/provider_protocol.py").is_file():
    raise RuntimeError(f"SAKURAMEDIA_HOST_ROOT 不是有效的宿主仓库: {HOST_ROOT}")
for path in (PLUGINS_ROOT, HOST_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
