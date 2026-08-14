#!/usr/bin/env python3
"""按 mime 类型排序文件列表，供 `tar -T` 使用，提升 zstd 压缩率。

原理：zstd 的 --long 长距离匹配窗口在"相似内容相邻"时命中率更高。
把同 mime 类型（.wasm/.js/.so/...）的文件聚在一起 → 重复模式更多 →
压缩率更高。目录条目先行，保证空目录/结构完整性。

用法：
    sort_by_mime.py <root_dir> [<list_file>]
        root_dir  要打包的目录（相对路径按此目录计算）
        list_file 输出文件（默认 stdout），每行一个相对路径

mime 判定：`file --mime-type -L -b` 批量调用（500/批，跟随符号链接）；
判定失败的文件回退 application/octet-stream。仅依赖标准库 + file 命令
（GitHub Actions ubuntu-latest 预装）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FALLBACK_MIME = "application/octet-stream"
BATCH = 500


def _file_mime_batch(paths: list[Path]) -> dict[Path, str]:
    """批量调用 file --mime-type 判定 mime；按输入顺序对齐输出。"""
    result: dict[Path, str] = {}
    try:
        proc = subprocess.run(
            ["file", "--mime-type", "-L", "-b", *(str(p) for p in paths)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {p: FALLBACK_MIME for p in paths}
    lines = proc.stdout.strip().splitlines()
    for idx, path in enumerate(paths):
        if idx < len(lines) and lines[idx]:
            result[path] = lines[idx].split(";")[0] or FALLBACK_MIME
        else:
            result[path] = FALLBACK_MIME
    return result


def sort_by_mime(root: Path, list_file: Path | None = None) -> int:
    """生成排序后的文件列表。返回输出行数。"""
    root = root.resolve()
    if not root.is_dir():
        raise SystemExit(f"error: not a directory: {root}")
    out = open(list_file, "w", encoding="utf-8") if list_file else sys.stdout

    dirs: list[str] = []
    files: list[Path] = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_dir():
            dirs.append(rel.as_posix())
        elif p.is_symlink() or p.is_file():
            files.append(p)

    mimes: dict[Path, str] = {}
    for i in range(0, len(files), BATCH):
        mimes.update(_file_mime_batch(files[i : i + BATCH]))

    count = 0
    for d in dirs:  # 目录先行：结构完整（含空目录），不影响 mime 聚合
        out.write(d + "\n")
        count += 1
    for path in sorted(files, key=lambda p: (mimes.get(p, FALLBACK_MIME), p.relative_to(root).as_posix())):
        out.write(path.relative_to(root).as_posix() + "\n")
        count += 1

    if list_file:
        out.close()
    return count


def main() -> int:
    if len(sys.argv) not in (2, 3):
        raise SystemExit(__doc__)
    root = Path(sys.argv[1])
    list_file = Path(sys.argv[2]) if len(sys.argv) == 3 else None
    sort_by_mime(root, list_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
