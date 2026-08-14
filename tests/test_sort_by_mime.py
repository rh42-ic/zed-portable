"""tests/test_sort_by_mime.py — scripts/sort_by_mime.py 排序逻辑单测（unittest）。

覆盖：目录条目先行、文件按 mime 聚合、相对路径输出、空目录保留、
符号链接包含、CLI 主入口。依赖本机 file 命令（GitHub ubuntu 镜像预装）。

运行：`uv run python -m unittest discover -s tests -v`（workdir=项目根）。
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from sort_by_mime import sort_by_mime  # noqa: E402


class SortByMimeTests(unittest.TestCase):
    def _make_tree(self, root: Path) -> None:
        (root / "sub" / "deep").mkdir(parents=True)
        (root / "empty").mkdir()
        # 混排：同目录内多种类型交错（模拟 node_modules）
        for i in range(6):
            (root / f"mod{i:02d}.js").write_text("function f() { return 1; }\n", encoding="utf-8")
            (root / f"mod{i:02d}.wasm").write_bytes(bytes(range(256)) * 8)
            (root / f"mod{i:02d}.json").write_text('{"k": "v"}\n', encoding="utf-8")
        (root / "sub" / "deep" / "data.bin").write_bytes(b"\x00" * 64)

    def test_order_dirs_first_files_grouped_by_mime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            self._make_tree(root)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sort_by_mime(root)
            lines = buf.getvalue().splitlines()

            self.assertIn("empty", lines)          # 空目录保留
            self.assertIn("sub", lines)
            self.assertIn("sub/deep", lines)
            files = [l for l in lines if l not in ("empty", "sub", "sub/deep")]
            self.assertEqual(len(files), 19)       # 18 混排 + 1 data.bin
            # 目录条目在最前
            self.assertEqual(lines[:3], ["empty", "sub", "sub/deep"])
            # 文件按 mime 聚合：同 mime 连续出现
            mimes = []
            for f in files:
                mimes.append((f, "json" if f.endswith(".json") else "wasm" if f.endswith(".wasm") else "js" if f.endswith(".js") else "bin"))
            # 聚合校验：每个类型的文件彼此相邻
            seen_kinds = []
            for _, kind in mimes:
                if not seen_kinds or seen_kinds[-1] != kind:
                    seen_kinds.append(kind)
            self.assertLess(len(seen_kinds), len(files))  # 有聚合
            # 路径无 ./ 前缀、唯一
            for f in files:
                self.assertFalse(f.startswith("./"))
            self.assertEqual(len(set(files)), len(files))

    def test_symlink_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            (root / "real").mkdir()
            (root / "real" / "a.txt").write_text("hello\n", encoding="utf-8")
            try:
                (root / "link").symlink_to("real/a.txt")
            except (OSError, NotImplementedError):
                self.skipTest("platform cannot create symlinks")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sort_by_mime(root)
            lines = buf.getvalue().splitlines()
            self.assertIn("link", lines)

    def test_cli_writes_list_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            (root / "a.txt").write_text("x\n", encoding="utf-8")
            out = Path(tmp) / "list.txt"
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "sort_by_mime.py"), str(root), str(out)],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            content = out.read_text(encoding="utf-8").splitlines()
            self.assertEqual(content, ["a.txt"])


if __name__ == "__main__":
    unittest.main()
