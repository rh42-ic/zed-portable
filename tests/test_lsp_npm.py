"""tests/test_lsp_npm.py — P5 npm LSP 的 $shared 符号链接替换单测（L1，零依赖 unittest）。

覆盖 lsp_npm._ensure_eslint_shared：vscode-eslint 归档里 client/src/shared 与
server/src/shared 是指向根 $shared/ 的符号链接，tarfile 在 Windows 上提取
symlink 不带 target_is_directory=True，生成文件类型链接，Node/TS 无法跟随 →
TS2307。本函数将链接替换为真实目录复制。

运行：`uv run python -m unittest discover -s tests -v`（workdir=项目根）。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zed_portable.lsp_npm import _ensure_eslint_shared  # noqa: E402


def _make_eslint_tree(root: Path) -> bool:
    """构造 vscode-eslint 归档解压后的结构：$shared 源目录 + 两个符号链接。"""
    shard = root / "$shared"
    shard.mkdir(parents=True)
    (shard / "customMessages.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (shard / "settings.ts").write_text("export type T = string;\n", encoding="utf-8")
    for rel in ("client/src/shared", "server/src/shared"):
        link = root / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to("../../$shared/", target_is_directory=True)
        except (OSError, NotImplementedError):
            # 平台不允许创建符号链接（如 Windows 无开发者模式）→ 测试直接跳过链接场景
            return False
    return True


class EnsureEslintSharedTests(unittest.TestCase):
    def test_replaces_symlinks_with_real_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vscode-eslint"
            if not _make_eslint_tree(root):
                self.skipTest("platform cannot create symlinks")
            for rel in ("client/src/shared", "server/src/shared"):
                self.assertTrue((root / rel).is_symlink())

            _ensure_eslint_shared(root)

            for rel in ("client/src/shared", "server/src/shared"):
                d = root / rel
                self.assertTrue(d.is_dir())
                self.assertFalse(d.is_symlink())
                self.assertTrue((d / "customMessages.ts").is_file())
                self.assertTrue((d / "settings.ts").is_file())
            # 源目录保留（webpack/shared 引用仍可用）
            self.assertTrue((root / "$shared" / "customMessages.ts").is_file())

    def test_replaces_broken_or_file_type_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vscode-eslint"
            shard = root / "$shared"
            shard.mkdir(parents=True)
            (shard / "settings.ts").write_text("export type T = string;\n", encoding="utf-8")
            link = root / "server" / "src" / "shared"
            link.parent.mkdir(parents=True)
            link.symlink_to("../../$shared/")  # 不传 target_is_directory 模拟 Windows 文件类型链接

            _ensure_eslint_shared(root)

            self.assertTrue(link.is_dir())
            self.assertFalse(link.is_symlink())
            self.assertTrue((link / "settings.ts").is_file())

    def test_keeps_existing_real_dir_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vscode-eslint"
            shard = root / "$shared"
            shard.mkdir(parents=True)
            (shard / "settings.ts").write_text("export type T = string;\n", encoding="utf-8")
            d = root / "server" / "src" / "shared"
            d.mkdir(parents=True)
            (d / "custom.ts").write_text("export const y = 2;\n", encoding="utf-8")

            _ensure_eslint_shared(root)

            self.assertTrue(d.is_dir())
            self.assertFalse(d.is_symlink())
            self.assertTrue((d / "custom.ts").is_file())  # 未被删改
            self.assertFalse((d / "settings.ts").exists())  # 未复制（已存在目录跳过）


if __name__ == "__main__":
    unittest.main()
