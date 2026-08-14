"""tests/test_finalize.py — P6 finalize 纯逻辑单测（L1，零依赖 unittest）。

覆盖 finalize.py 的私有/纯函数：_merge_settings / _build_info_text /
_assert_products，以及 finalize 全流程（settings.json、run.sh/run.ps1、
BUILD_INFO、体积报告）。不联网、不做真实安装、快速（<30s）。

运行：`uv run python -m unittest discover -s tests -v`（workdir=项目根）。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# 包在 src/ 布局：unittest discover 从项目根跑时，tests 内直接 import
# zed_onprem_bundle 会失败——此处把项目根/src 插入 sys.path（最简单自足方案）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zed_onprem_bundle import finalize as finalize_mod
from zed_onprem_bundle import node as node_mod
from zed_onprem_bundle import toolchain as toolchain_mod
from zed_onprem_bundle.finalize import (
    _assert_products,
    _build_info_text,
    _merge_settings,
)

#: settings.json 基准内容（与 finalize._merge_settings 的 base 一致，供断言）
BASE_SETTINGS = {
    "telemetry": {"metrics": False, "diagnostics": False},
    "auto_update": False,
}


def _fake_tc():
    """构造 Toolchain 假实例（路径不落地，仅 _build_info_text 读 zed_tag/zed_commit）。"""
    return toolchain_mod.Toolchain(
        wasi_sdk_path=Path("/nonexistent/wasi-sdk"),
        zed_extension=Path("/nonexistent/zed-extension"),
        zed_bin=Path("/nonexistent/zed"),
        zed_tag="v0.180.0",
        zed_commit="abc123def",
    )


def _lines(text):
    """BUILD_INFO 文本 → {key: value}。"""
    return dict(line.split("=", 1) for line in text.strip().splitlines())


class MergeSettingsTests(unittest.TestCase):
    """_merge_settings：base 默认恒存在；cfg.settings 深覆盖且不丢其他 base 键。"""

    def test_empty_cfg_only_base(self):
        """settings 缺失 → 结果与 base 完全一致。"""
        self.assertEqual(_merge_settings({}), BASE_SETTINGS)

    def test_base_defaults_present(self):
        """base 键恒存在：telemetry.metrics/diagnostics=False、auto_update=False。"""
        merged = _merge_settings({})
        self.assertIs(merged["telemetry"]["metrics"], False)
        self.assertIs(merged["telemetry"]["diagnostics"], False)
        self.assertIs(merged["auto_update"], False)

    def test_deep_override_keeps_other_base_keys(self):
        """嵌套 dict 覆盖 metrics 时，同层其他键 diagnostics 仍为 base 值。"""
        merged = _merge_settings({"settings": {"telemetry": {"metrics": True}}})
        self.assertIs(merged["telemetry"]["metrics"], True)
        self.assertIs(merged["telemetry"]["diagnostics"], False)
        self.assertIs(merged["auto_update"], False)

    def test_arbitrary_nested_keys_merged(self):
        """cfg.settings 任意嵌套 dict 并入，且不丢 base 键。"""
        merged = _merge_settings(
            {"settings": {"theme": {"dark": True}, "lsp": {"pyright": {"x": 1}}}}
        )
        self.assertEqual(merged["theme"], {"dark": True})
        self.assertEqual(merged["lsp"], {"pyright": {"x": 1}})
        self.assertEqual(merged["telemetry"], BASE_SETTINGS["telemetry"])

    def test_full_override(self):
        """全部键覆盖（auto_update/telemetry 全量）。"""
        merged = _merge_settings(
            {"settings": {"auto_update": True, "telemetry": {"metrics": True, "diagnostics": True}}}
        )
        self.assertEqual(
            merged,
            {"telemetry": {"metrics": True, "diagnostics": True}, "auto_update": True},
        )

    def test_non_dict_settings_tolerated(self):
        """cfg.settings 非 dict（None/str/int）→ 容错返回 base。"""
        for bad in (None, "nope", 42, ["list"]):
            with self.subTest(bad=bad):
                self.assertEqual(_merge_settings({"settings": bad}), BASE_SETTINGS)

    def test_repeated_calls_no_mutation_of_base(self):
        """多次调用互不污染（base 每调用重建；回归：浅拷贝共享内层 dict 的 bug）。"""
        first = _merge_settings({"settings": {"telemetry": {"metrics": True}}})
        second = _merge_settings({})
        self.assertIs(first["telemetry"]["metrics"], True)
        self.assertIs(second["telemetry"]["metrics"], False)


class BuildInfoTextTests(unittest.TestCase):
    """_build_info_text：字段齐全、warnings 恒输出、extensions_commit 优先级。"""

    def setUp(self):
        self.tc = _fake_tc()

    def test_fields_present(self):
        """bundle_version/zed_commit/platform/build_date/config_files/warnings 字段齐全。"""
        text = _build_info_text(
            {}, self.tc, "linux-x64", ["a.toml", "b.toml"], ["gh1"], ["npm1"], ["ext1"]
        )
        lines = _lines(text)
        self.assertEqual(lines["bundle_version"], "v0.180.0")
        self.assertEqual(lines["zed_commit"], "abc123def")
        self.assertEqual(lines["platform"], "linux-x64")
        self.assertTrue(lines["build_date"], "build_date 应非空")
        self.assertEqual(lines["config_files"], "a.toml,b.toml")
        self.assertEqual(lines["warnings"], "gh1,npm1,ext1")

    def test_warnings_line_always_present(self):
        """warnings 行恒输出（无失败/跳过时值为空串）。"""
        text = _build_info_text({}, self.tc, "linux-x64", [], [], [], [])
        self.assertIn("warnings=", text)
        self.assertEqual(_lines(text)["warnings"], "")

    def test_config_files_basename_only(self):
        """config_files 取 basename（传完整路径也不含目录部分）。"""
        text = _build_info_text(
            {}, self.tc, "linux-x64", ["/x/y/a.toml", "b.toml"], [], [], [], env={}
        )
        self.assertEqual(_lines(text)["config_files"], "a.toml,b.toml")

    def test_env_rev_priority(self):
        """env EXTENSIONS_REV 优先于 cfg extensions.rev。"""
        text = _build_info_text(
            {"extensions": {"rev": "cfgrev"}}, self.tc, "linux-x64", [], [], [], [],
            env={"EXTENSIONS_REV": "envrev"},
        )
        self.assertEqual(_lines(text)["extensions_commit"], "envrev")

    def test_cfg_rev_secondary(self):
        """cfg extensions.rev 次优先（env 无 EXTENSIONS_REV 时）。"""
        text = _build_info_text(
            {"extensions": {"rev": "cfgrev"}}, self.tc, "linux-x64", [], [], [], [], env={}
        )
        self.assertEqual(_lines(text)["extensions_commit"], "cfgrev")

    def test_git_fallback_unknown_on_failure(self):
        """env 与 cfg 均空 → git rev-parse 兜底；仓库不存在 → "unknown"。"""
        text = _build_info_text(
            {}, self.tc, "linux-x64", [], [], [], [],
            env={"EXTENSIONS_REPO": "/nonexistent/extensions-repo"},
        )
        self.assertEqual(_lines(text)["extensions_commit"], "unknown")

    def test_env_repo_success(self):
        """git rev-parse 成功 → 返回仓库 HEAD sha（env EXTENSIONS_REPO 指向真仓库）。"""
        import subprocess

        repo = Path(tempfile.mkdtemp(prefix="ext_repo_"))
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        # 裸手写 .git/ 不会被 git 识别（"not a git repository"），需先 git init
        proc = subprocess.run(
            ["git", "init", "-q", str(repo)], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(proc.returncode, 0, "git init 应成功（本机需有 git）")
        (repo / ".git" / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (repo / ".git" / "refs" / "heads" / "main").write_text(
            "deadbeef00000000000000000000000000000000\n"
        )
        text = _build_info_text(
            {}, self.tc, "linux-x64", [], [], [], [],
            env={"EXTENSIONS_REPO": str(repo)},
        )
        self.assertEqual(_lines(text)["extensions_commit"], "deadbeef00000000000000000000000000000000")


class AssertProductsTests(unittest.TestCase):
    """_assert_products：缺失 raise 并列出；failed/skipped 内项不断言；gopls 不断言。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="finalize_assert_"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._langs = self._tmp / "data" / "languages"
        self._langs.mkdir(parents=True)
        (self._tmp / "data" / "extensions" / "installed").mkdir(parents=True)
        (self._tmp / "bin").mkdir()
        self.np = node_mod.NodePaths(
            node_bin=self._tmp / "bin" / "node",
            npm_cmd=self._tmp / "bin" / "npm",
        )
        # 启用 rust-analyzer + gopls（github），yaml + pyright（npm），两个扩展
        self.cfg = {
            "lsp": {
                "github": {"rust-analyzer": True, "gopls": True, "ruff": False},
                "npm": {"yaml": True, "pyright": True},
            },
            "extensions": {"ids": ["foo-ext", "bar-ext"]},
        }

    def _install_all(self):
        """造齐全部产物（真实文件名，与 P4/P5 落位规则一致）。"""
        self.np.node_bin.touch()
        ra_dir = self._langs / "rust-analyzer"
        ra_dir.mkdir()
        (ra_dir / "rust-analyzer-2025-08-04").touch()
        for rel in (
            "yaml-language-server/node_modules/yaml-language-server/bin/yaml-language-server",
            "pyright/node_modules/pyright/langserver.index.js",
        ):
            p = self._langs / rel
            p.parent.mkdir(parents=True)
            p.touch()
        for eid in ("foo-ext", "bar-ext"):
            d = self._tmp / "data" / "extensions" / "installed" / eid
            d.mkdir()
            (d / "extension.toml").touch()
            (d / "extension.wasm").touch()

    def test_missing_raises_with_all_items(self):
        """全缺失 → AssertionError 且消息列出全部缺失项。"""
        with self.assertRaises(AssertionError) as ctx:
            _assert_products(self.cfg, "linux-x64", self._tmp, self.np, [], [], [])
        msg = str(ctx.exception)
        for expected in ("node 运行时", "rust-analyzer", "yaml", "pyright", "foo-ext", "bar-ext"):
            self.assertIn(expected, msg)

    def test_all_present_passes(self):
        """产物齐备 → 不抛异常。"""
        self._install_all()
        _assert_products(self.cfg, "linux-x64", self._tmp, self.np, [], [], [])

    def test_disabled_server_not_asserted(self):
        """cfg 中 False 的 server（ruff）不被断言。"""
        with self.assertRaises(AssertionError) as ctx:
            _assert_products(self.cfg, "linux-x64", self._tmp, self.np, [], [], [])
        self.assertNotIn("ruff", str(ctx.exception))

    def test_gopls_not_asserted(self):
        """gopls 不断言：缺失项消息中不含 gopls。"""
        self.np.node_bin.touch()
        with self.assertRaises(AssertionError) as ctx:
            _assert_products(self.cfg, "linux-x64", self._tmp, self.np, [], [], [])
        self.assertNotIn("gopls", str(ctx.exception))

    def test_failed_lists_skip_assertion(self):
        """failed_gh/failed_npm/skipped_exts 内项不断言 → 部分安装通过。"""
        self.np.node_bin.touch()
        yaml_bin = self._langs / "yaml-language-server" / "node_modules" / "yaml-language-server" / "bin" / "yaml-language-server"
        yaml_bin.parent.mkdir(parents=True)
        yaml_bin.touch()
        bar = self._tmp / "data" / "extensions" / "installed" / "bar-ext"
        bar.mkdir()
        (bar / "extension.toml").touch()
        (bar / "extension.wasm").touch()
        _assert_products(
            self.cfg, "linux-x64", self._tmp, self.np,
            failed_gh=["rust-analyzer"],
            failed_npm=["pyright"],
            skipped_exts=["foo-ext"],
        )

    def test_node_bin_missing_alone_fails(self):
        """唯一缺 node → 报错信息包含 node 运行时且不含其他项。"""
        self._install_all()
        self.np.node_bin.unlink()
        with self.assertRaises(AssertionError) as ctx:
            _assert_products(self.cfg, "linux-x64", self._tmp, self.np, [], [], [])
        msg = str(ctx.exception)
        self.assertIn("node 运行时", msg)
        self.assertNotIn("rust-analyzer", msg)
        self.assertNotIn("yaml", msg)


class FinalizeFlowTests(unittest.TestCase):
    """finalize 全流程：settings.json / run.sh（可执行位）/ run.ps1 / BUILD_INFO / 体积报告。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="finalize_flow_"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.dist = self._tmp / "dist"
        # 假 node 运行时：断言需要真实存在的文件
        self.np = node_mod.NodePaths(
            node_bin=self._tmp / "node" / "node",
            npm_cmd=self._tmp / "node" / "npm",
        )
        self.np.node_bin.parent.mkdir(parents=True)
        self.np.node_bin.touch()
        self.tc = _fake_tc()

    def test_empty_cfg_full_flow(self):
        """空 cfg 全流程不崩：settings.json 内容正确、脚本与 BUILD_INFO 生成。"""
        finalize_mod.finalize({}, "linux-x64", self.dist, self.tc, self.np, [], [], [], [])
        settings = json.loads((self.dist / "data" / "config" / "settings.json").read_text())
        self.assertEqual(settings, BASE_SETTINGS)
        self.assertTrue((self.dist / "run.sh").is_file())
        self.assertFalse((self.dist / "run.ps1").exists(), "linux 平台不应生成 run.ps1")
        self.assertTrue((self.dist / "BUILD_INFO").is_file())

    def test_stale_other_platform_script_removed(self):
        """增量重跑：上一平台残留脚本被清理（linux 流程清 run.ps1）。"""
        self.dist.mkdir(parents=True, exist_ok=True)
        (self.dist / "run.ps1").write_text("stale")
        finalize_mod.finalize({}, "linux-x64", self.dist, self.tc, self.np, [], [], [], [])
        self.assertFalse((self.dist / "run.ps1").exists(), "残留 run.ps1 应被删除")

    def test_run_sh_content_and_exec_bit(self):
        """run.sh：可执行位 + exec 指向 bin/zed 且 --user-data-dir 指向 data/。"""
        finalize_mod.finalize({}, "linux-x64", self.dist, self.tc, self.np, [], [], [], [])
        run_sh = self.dist / "run.sh"
        self.assertTrue(os.access(run_sh, os.X_OK), "run.sh 应带可执行位")
        content = run_sh.read_text()
        self.assertIn('exec "$(dirname "$(readlink -f "$0")")/bin/zed"', content)
        self.assertIn('--user-data-dir "$(dirname "$(readlink -f "$0")")/data"', content)
        self.assertIn('"$@"', content)

    def test_run_ps1_content(self):
        """run.ps1（windows 平台）：Split-Path 根 + bin\\zed.exe + @args；不生成 run.sh。"""
        finalize_mod.finalize({}, "windows-x64", self.dist, self.tc, self.np, [], [], [], [])
        content = (self.dist / "run.ps1").read_text()
        self.assertIn("Split-Path -Parent $MyInvocation.MyCommand.Path", content)
        self.assertIn('bin\\zed.exe', content)
        self.assertIn("--user-data-dir", content)
        self.assertIn("@args", content)
        self.assertFalse((self.dist / "run.sh").exists(), "windows 平台不应生成 run.sh")

    def test_unknown_platform_raises(self):
        """未知平台 → ValueError。"""
        with self.assertRaises(ValueError):
            finalize_mod.finalize({}, "macos-x64", self.dist, self.tc, self.np, [], [], [], [])

    def test_settings_and_build_info_override(self):
        """cfg.settings 覆盖 + config_files/warnings 进 BUILD_INFO。"""
        cfg = {"settings": {"telemetry": {"metrics": True}, "theme": {"dark": True}}}
        finalize_mod.finalize(
            cfg, "linux-x64", self.dist, self.tc, self.np,
            ["gh1"], [], ["ext1"], ["a.toml"],
        )
        settings = json.loads((self.dist / "data" / "config" / "settings.json").read_text())
        self.assertIs(settings["telemetry"]["metrics"], True)
        self.assertIs(settings["telemetry"]["diagnostics"], False)
        self.assertEqual(settings["theme"], {"dark": True})
        bi = (self.dist / "BUILD_INFO").read_text()
        self.assertIn("config_files=a.toml", bi)
        self.assertIn("warnings=gh1,ext1", bi)

    def test_assert_failure_raises_in_flow(self):
        """node 二进制缺失 → finalize 内 _assert_products raise AssertionError。"""
        self.np.node_bin.unlink()
        with self.assertRaises(AssertionError):
            finalize_mod.finalize({}, "linux-x64", self.dist, self.tc, self.np, [], [], [], [])

    def test_idempotent_rerun(self):
        """同参数重跑幂等：settings.json 内容不变、不抛异常。"""
        finalize_mod.finalize({}, "linux-x64", self.dist, self.tc, self.np, [], [], [], [])
        first = (self.dist / "data" / "config" / "settings.json").read_text()
        finalize_mod.finalize({}, "linux-x64", self.dist, self.tc, self.np, [], [], [], [])
        second = (self.dist / "data" / "config" / "settings.json").read_text()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
