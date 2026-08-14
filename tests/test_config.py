"""config.py 合并规则测试（§4.2 规则 1-7 + 结构规范化回归）。

全部使用真实 toml 文件 + 临时目录（tempfile），不 mock 文件系统。
运行：uv run python -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from zed_portable import config as cfgmod
from zed_portable.config import load_merged_config

LOGGER = "zed_portable.config"


class ConfigMergeTest(unittest.TestCase):
    """辅助：把 {文件名: toml 内容} 写进临时 config_dir/enabled/。"""

    def make_config_dir(self, files: dict[str, str]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        enabled = Path(tmp.name) / "enabled"
        enabled.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (enabled / name).write_text(content, encoding="utf-8")
        return Path(tmp.name)

    def test_array_append_and_string_dedup(self):
        """数组追加 + 字符串精确去重：两文件 ids 重叠 'svelte'，只保留一次且保序。"""
        cfg_dir = self.make_config_dir({
            "10-a.toml": '[extensions]\nids = ["vue", "svelte"]\n',
            "20-b.toml": '[extensions]\nids = ["svelte", "rust"]\n',
        })
        cfg = load_merged_config(cfg_dir)
        self.assertEqual(cfg["extensions"]["ids"], ["vue", "svelte", "rust"])

    def test_deep_merge_and_scalar_override(self):
        """表 deep merge（键级覆盖）+ 标量覆盖：后排序文件优先，defaults 未涉及键保留。"""
        cfg_dir = self.make_config_dir({
            "10-a.toml": (
                '[zed]\nchannel = "preview"\n'
                '[lsp.github]\n"rust-analyzer" = true\n'
            ),
            "20-b.toml": (
                '[zed]\nchannel = "dev"\n'
                '[lsp.github]\n"rust-analyzer" = false\ntypst = true\n'
            ),
        })
        cfg = load_merged_config(cfg_dir)
        self.assertEqual(cfg["zed"]["channel"], "dev")          # 后文件覆盖前文件
        self.assertEqual(cfg["zed"]["binary"], "download")      # defaults 保留
        self.assertEqual(cfg["lsp"]["github"],                  # 表键级覆盖
                         {"rust-analyzer": False, "typst": True})

    def test_nested_settings_deep_merge(self):
        """settings 任意嵌套 dict 递归合并（键级覆盖，非整表替换）。"""
        cfg_dir = self.make_config_dir({
            "10-a.toml": '[settings.editor]\ntab_size = 2\nfont_size = 12\n',
            "20-b.toml": '[settings.editor]\nfont_size = 14\n',
        })
        cfg = load_merged_config(cfg_dir)
        self.assertEqual(cfg["settings"]["editor"], {"tab_size": 2, "font_size": 14})

    def test_empty_enabled_defaults_and_normalization(self):
        """空 enabled → 兜底默认 + 结构规范化回归（用户改动点）：
        extensions.ids==[]、extensions.rev==""、lsp.github=={}、lsp.npm=={}；
        remote_server 不在默认中（不链接 remote.toml = 不下载远程服务端）。"""
        cfg_dir = self.make_config_dir({})
        cfg = load_merged_config(cfg_dir)
        self.assertEqual(cfg["zed"], {"channel": "stable", "release_tag": "", "binary": "download"})
        self.assertEqual(cfg["extensions"], {"rev": "", "ids": []})
        self.assertEqual(cfg["lsp"], {"github": {}, "npm": {}})
        self.assertNotIn("remote_server", cfg)

    def test_normalization_fills_missing_keys(self):
        """部分定义时补齐缺失键：仅给 ids → rev 补 ""；仅给 lsp.github → npm 补 {}。"""
        cfg_dir = self.make_config_dir({
            "10-a.toml": '[extensions]\nids = ["vue"]\n[lsp.github]\n"rust-analyzer" = true\n',
        })
        cfg = load_merged_config(cfg_dir)
        self.assertEqual(cfg["extensions"], {"ids": ["vue"], "rev": ""})
        self.assertEqual(cfg["lsp"], {"github": {"rust-analyzer": True}, "npm": {}})

    def test_env_override_nonempty_only(self):
        """env 覆盖：非空生效；空串/缺失不覆盖（保留文件值）。"""
        cfg_dir = self.make_config_dir({
            "10-a.toml": '[zed]\nrelease_tag = "v0.1.0"\n[extensions]\nrev = "from-file"\n',
        })
        # 非空 → 覆盖
        cfg = load_merged_config(cfg_dir, env={
            "ZED_RELEASE_TAG": "v0.180.0", "EXTENSIONS_REV": "abc123",
        })
        self.assertEqual(cfg["zed"]["release_tag"], "v0.180.0")
        self.assertEqual(cfg["extensions"]["rev"], "abc123")
        # 空值/缺失 → 不覆盖
        cfg2 = load_merged_config(cfg_dir, env={"ZED_RELEASE_TAG": "", "EXTENSIONS_REV": None})
        self.assertEqual(cfg2["zed"]["release_tag"], "v0.1.0")
        self.assertEqual(cfg2["extensions"]["rev"], "from-file")

    def test_ids_containing_non_str_raises(self):
        """校验：extensions.ids 含非 str 元素 → ValueError。"""
        cfg_dir = self.make_config_dir({"10-a.toml": '[extensions]\nids = ["vue", 42]\n'})
        with self.assertRaises(ValueError):
            load_merged_config(cfg_dir)

    def test_ids_scalar_raises(self):
        """校验：extensions.ids 非 list（标量）→ ValueError。"""
        cfg_dir = self.make_config_dir({"10-a.toml": '[extensions]\nids = "oops"\n'})
        with self.assertRaises(ValueError):
            load_merged_config(cfg_dir)

    def test_lsp_non_bool_ignored_with_warning(self):
        """校验：lsp.github 值非 bool → 忽略 + 告警；bool 值保留。"""
        cfg_dir = self.make_config_dir({
            "10-a.toml": '[lsp.github]\n"rust-analyzer" = "yes"\ntypst = true\n',
        })
        with self.assertLogs(LOGGER, level="WARNING") as cm:
            cfg = load_merged_config(cfg_dir)
        self.assertEqual(cfg["lsp"]["github"], {"typst": True})
        self.assertTrue(any("非 bool" in msg for msg in cm.output))

    def test_unknown_top_key_ignored_with_warning(self):
        """未知顶层键 → 忽略 + 告警，不崩溃、不进入结果。"""
        cfg_dir = self.make_config_dir({
            "10-a.toml": '[zed]\nchannel = "dev"\n[unknown_top]\nx = 1\n',
        })
        with self.assertLogs(LOGGER, level="WARNING") as cm:
            cfg = load_merged_config(cfg_dir)
        self.assertNotIn("unknown_top", cfg)
        self.assertEqual(cfg["zed"]["channel"], "dev")
        self.assertTrue(any("未知顶层键" in msg for msg in cm.output))

    def test_sources_recorded(self):
        """_sources 存在且记录各顶层键来源文件（含 defaults 标记）。"""
        cfg_dir = self.make_config_dir({
            "10-a.toml": '[zed]\nchannel = "preview"\n',
            "20-b.toml": '[extensions]\nids = ["vue"]\n',
        })
        cfg = load_merged_config(cfg_dir)
        self.assertIn("_sources", cfg)
        self.assertEqual(cfg["_sources"]["zed"], ["10-a.toml", "defaults"])
        self.assertEqual(cfg["_sources"]["extensions"], ["20-b.toml"])

    def test_missing_config_dir_returns_defaults(self):
        """config_dir 不存在（无 enabled 子目录）→ 兜底默认，不崩溃。"""
        missing = Path(tempfile.mkdtemp()) / "no-such-config"
        cfg = load_merged_config(missing)
        self.assertEqual(cfg["zed"]["channel"], "stable")
        self.assertEqual(cfg["extensions"], {"rev": "", "ids": []})
        self.assertEqual(cfg["lsp"], {"github": {}, "npm": {}})
        self.assertNotIn("remote_server", cfg)

    def test_symlinked_toml_loaded(self):
        """enabled 下软链接 toml 直接读内容生效（无需解析链接本身）。"""
        with tempfile.TemporaryDirectory() as real_td, tempfile.TemporaryDirectory() as cfg_td:
            real = Path(real_td) / "real.toml"
            real.write_text('[zed]\nchannel = "dev"\n', encoding="utf-8")
            cfg_dir = Path(cfg_td)
            enabled = cfg_dir / "enabled"
            enabled.mkdir()
            (enabled / "link.toml").symlink_to(real)
            cfg = load_merged_config(cfg_dir)
            self.assertEqual(cfg["zed"]["channel"], "dev")
            self.assertIn("link.toml", cfg["_sources"]["zed"])

    def test_defaults_module_constant(self):
        """DEFAULT_CONFIG 常量结构与契约一致（remote_server 不在默认中——
        不链接 = 不下载远程服务端）。"""
        self.assertEqual(
            cfgmod.DEFAULT_CONFIG,
            {
                "zed": {"channel": "stable", "release_tag": "", "binary": "download"},
            },
        )

    def test_remote_server_from_remote_preset(self):
        """链接 config/available/remote.toml → remote_server 启用：
        platforms 全 6、source github（独立 preset，不跟随 core-zed）。"""
        preset_path = Path(__file__).resolve().parents[1] / "config" / "available" / "remote.toml"
        preset = preset_path.read_text(encoding="utf-8")
        cfg_dir = self.make_config_dir({"remote.toml": preset})
        cfg = load_merged_config(cfg_dir)
        self.assertEqual(cfg["remote_server"]["platforms"], list(cfgmod.SUPPORTED_PLATFORMS))
        self.assertEqual(cfg["remote_server"]["source"], "github")

    def test_remote_server_invalid_platform_raises(self):
        """校验：remote_server.platforms 含 SUPPORTED_PLATFORMS 外平台 → ValueError。"""
        cfg_dir = self.make_config_dir({
            "10-a.toml": '[remote_server]\nplatforms = ["linux-ppc64"]\n',
        })
        with self.assertRaises(ValueError) as ctx:
            load_merged_config(cfg_dir)
        self.assertIn("linux-ppc64", str(ctx.exception))

    def test_remote_server_non_github_source_raises(self):
        """校验：remote_server.source 非 github → ValueError（当前仅支持 github）。"""
        cfg_dir = self.make_config_dir({
            "10-a.toml": '[remote_server]\nsource = "gitlab"\n',
        })
        with self.assertRaises(ValueError) as ctx:
            load_merged_config(cfg_dir)
        self.assertIn("github", str(ctx.exception))

    def test_remote_server_platforms_env_override(self):
        """env REMOTE_SERVER_PLATFORMS（逗号分隔）→ 独立创建 remote_server 键并
        整体替换 platforms 为 2 个（strip 生效）——env 可独立启用，无需链接 preset。"""
        cfg_dir = self.make_config_dir({})
        cfg = load_merged_config(cfg_dir, env={
            "REMOTE_SERVER_PLATFORMS": "linux-x86_64, windows-x86_64",
        })
        self.assertEqual(cfg["remote_server"]["platforms"], ["linux-x86_64", "windows-x86_64"])


if __name__ == "__main__":
    unittest.main()
