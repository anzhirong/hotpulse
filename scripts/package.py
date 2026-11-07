"""把插件目录打成可上传的 zip。

为什么不用 `python -m zipfile -c`：它会连 `__pycache__`、本地 `data/` 一起打进去，
而且目录里混进本地订阅数据（含你的关键词、会话 id）后，包会被带到服务器上。
PowerShell 的 Compress-Archive 更不能用——它写反斜杠路径，Linux 解压会把文件散成
`astrbot_plugin_hotpulse\\main.py` 这种名字。

用法：
    python scripts/package.py
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "astrbot_plugin_hotpulse"
OUT = ROOT / "dist" / "astrbot_plugin_hotpulse.zip"

# 这些是本地运行时产物，不该进包
EXCLUDE_DIRS = {"__pycache__", "data", ".pytest_cache", ".ruff_cache"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".tmp"}


def collect() -> list[Path]:
    files = []
    for path in sorted(PLUGIN.rglob("*")):
        if path.is_dir():
            continue
        if set(path.relative_to(PLUGIN).parts) & EXCLUDE_DIRS:
            continue
        if path.suffix in EXCLUDE_SUFFIX:
            continue
        files.append(path)
    return files


def main() -> int:
    if not (PLUGIN / "main.py").exists():
        print(f"插件目录不完整：{PLUGIN}", file=sys.stderr)
        return 1

    files = collect()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            # 统一成 posix 风格，保证 Linux 上解压出来是一棵正常的树
            arcname = Path(PLUGIN.name, *path.relative_to(PLUGIN).parts).as_posix()
            zf.write(path, arcname)

    print(f"{OUT.relative_to(ROOT)}  {OUT.stat().st_size / 1024:.1f} KB  {len(files)} 个文件")
    for path in files:
        print("  ", Path(PLUGIN.name, *path.relative_to(PLUGIN).parts).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
