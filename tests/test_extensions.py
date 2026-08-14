"""tests/test_extensions.py — P2 扩展获取逻辑单测（L1，零网络，unittest 风格）。

覆盖 extensions.py 主路径（官方 API 直下）与兜底路径（本地打包）：
- _latest_compatible_version 兼容过滤（schema_version / wasm_api_version / semver 最大）
- 幂等版本比对（同版本零下载；toml 解析失败保守跳过）
- 版本不等 → 下载 + 解压 + 原子落位
- 元数据失败 / 下载失败 → 本地打包兜底（submodule 流程）
- 兜底工具链惰性回填（tc 字段 None 时 ensure_wasi_sdk/ensure_zed_extension_cli 回填）

运行：uv run pytest tests/test_extensions.py -q（或全量 uv run pytest -q）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zed_portable import download as download_mod
from zed_portable import extensions as ext_mod
from zed_portable import toolchain as toolchain_mod

API = ext_mod.ZED_EXTENSIONS_API


def _tc():
    return toolchain_mod.Toolchain(
        zed_bin=Path("/nonexistent/zed"),
        zed_tag="v1.0.0",
        zed_commit="abc123def",
        wasi_sdk_path=None,
        zed_extension=None,
    )


def _ext_dir(dist: Path, eid: str) -> Path:
    d = dist / "data" / "extensions" / "installed" / eid
    d.mkdir(parents=True, exist_ok=True)
    return d


class FakeProc:
    returncode = 0
    stdout = ""
    stderr = ""


class LatestCompatibleVersionTests(unittest.TestCase):
    """兼容过滤：wasm_api_version / schema_version 超限排除；缺失视为兼容；semver 最大优先。"""

    def test_excludes_high_wasm_api_version(self):
        entries = [{"version": "1.0.0", "schema_version": 1, "wasm_api_version": "0.9.0"}]
        self.assertIsNone(ext_mod._latest_compatible_version(entries))

    def test_wasm_api_boundary_equal_compatible(self):
        entries = [{"version": "1.0.0", "schema_version": 1, "wasm_api_version": "0.7.0"}]
        self.assertEqual(ext_mod._latest_compatible_version(entries), "1.0.0")

    def test_excludes_high_schema_version(self):
        entries = [{"version": "1.0.0", "schema_version": 2}]
        self.assertIsNone(ext_mod._latest_compatible_version(entries))

    def test_missing_schema_and_wasm_default_compatible(self):
        """schema_version 缺省 0、wasm_api_version 缺失 → 兼容。"""
        entries = [{"version": "1.0.0"}]
        self.assertEqual(ext_mod._latest_compatible_version(entries), "1.0.0")

    def test_semver_max_priority(self):
        entries = [
            {"version": "1.0.0", "schema_version": 1},
            {"version": "1.2.0", "schema_version": 1},
            {"version": "1.2.1", "schema_version": 1},
        ]
        self.assertEqual(ext_mod._latest_compatible_version(entries), "1.2.1")

    def test_mixed_filtering_prefers_compatible(self):
        entries = [
            {"version": "2.0.0", "schema_version": 1, "wasm_api_version": "0.9.0"},
            {"version": "1.5.0", "schema_version": 1},
        ]
        self.assertEqual(ext_mod._latest_compatible_version(entries), "1.5.0")

    def test_empty_data_none(self):
        self.assertIsNone(ext_mod._latest_compatible_version([]))

    def test_non_dict_entries_tolerated(self):
        entries = ["junk", None, {"version": "1.0.0", "schema_version": 1}]
        self.assertEqual(ext_mod._latest_compatible_version(entries), "1.0.0")


class InstallFlowTests(unittest.TestCase):
    """build_extensions 主/兜底路径（mock download 模块，零网络）。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="ext_flow_"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.build = self._tmp / "build"
        self.dist = self._tmp / "dist"
        self.repo = self._tmp / "extensions-repo"
        self.repo.mkdir()
        self.cfg = {"extensions": {"ids": ["foo"]}}
        self.tc = _tc()

    def test_same_version_zero_download(self):
        """installed toml 版本 == 最新兼容版 → 跳过，download_file 零调用。"""
        _ext_dir(self.dist, "foo")
        (self.dist / "data" / "extensions" / "installed" / "foo" / "extension.toml").write_text(
            "name = 'foo'\nversion = '1.2.3'\n"
        )
        with mock.patch.object(
            download_mod, "get_json", return_value={"data": [{"version": "1.2.3", "schema_version": 1}]}
        ), mock.patch.object(download_mod, "download_file") as dl:
            skipped = ext_mod.build_extensions(
                self.cfg, self.tc, "linux-x64", self.build, self.dist, "/nonexistent/repo"
            )
        self.assertEqual(skipped, [])
        dl.assert_not_called()

    def test_unparseable_toml_conservative_skip(self):
        """toml 解析失败 → 保守视为已装，跳过（不重装、不下载）。"""
        _ext_dir(self.dist, "foo")
        (self.dist / "data" / "extensions" / "installed" / "foo" / "extension.toml").write_bytes(
            b"not-valid-toml[[["
        )
        with mock.patch.object(
            download_mod, "get_json", return_value={"data": [{"version": "1.2.3", "schema_version": 1}]}
        ), mock.patch.object(download_mod, "download_file") as dl:
            skipped = ext_mod.build_extensions(
                self.cfg, self.tc, "linux-x64", self.build, self.dist, "/nonexistent/repo"
            )
        self.assertEqual(skipped, [])
        dl.assert_not_called()

    def test_version_mismatch_triggers_download_extract_place(self):
        """未安装/版本不等 → 下载官方产物 + 解压 + 原子落位。"""

        def _fake_download(url, dest, **kw):
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(b"tar-archive")

        def _fake_extract(archive, dest_dir):
            dest_dir = Path(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / "extension.toml").write_text("name = 'foo'\nversion = '1.2.3'\n")
            (dest_dir / "extension.wasm").write_bytes(b"\0asm")

        with mock.patch.object(
            download_mod, "get_json", return_value={"data": [{"version": "1.2.3", "schema_version": 1}]}
        ), mock.patch.object(download_mod, "download_file", side_effect=_fake_download) as dl, \
             mock.patch.object(download_mod, "extract_archive", side_effect=_fake_extract):
            skipped = ext_mod.build_extensions(
                self.cfg, self.tc, "linux-x64", self.build, self.dist, "/nonexistent/repo"
            )
        self.assertEqual(skipped, [])
        dl.assert_called_once_with(
            f"{API}/foo/1.2.3/download",
            self.build / "ext" / "archives" / "foo-1.2.3.tar.gz",
        )
        installed = self.dist / "data" / "extensions" / "installed" / "foo"
        self.assertTrue((installed / "extension.toml").is_file())
        self.assertTrue((installed / "extension.wasm").is_file())

    def test_metadata_failure_falls_back_to_submodule(self):
        """元数据 GET 失败 → 本地打包兜底（submodule 流程全通过 → 不跳过）。"""
        with mock.patch.object(download_mod, "get_json", side_effect=download_mod.DownloadError("net")), \
             mock.patch.object(ext_mod, "_init_submodule", return_value=True) as sub, \
             mock.patch.object(ext_mod, "_package_extension", return_value=True), \
             mock.patch.object(ext_mod, "_place_extension", return_value=True):
            skipped = ext_mod.build_extensions(
                self.cfg, self.tc, "linux-x64", self.build, self.dist, str(self.repo)
            )
        self.assertEqual(skipped, [])
        sub.assert_called_once()

    def test_fallback_submodule_failure_skipped(self):
        """兜底 submodule init 失败 → 该扩展进跳过清单。"""
        with mock.patch.object(download_mod, "get_json", side_effect=download_mod.DownloadError("net")), \
             mock.patch.object(ext_mod, "_init_submodule", return_value=False):
            skipped = ext_mod.build_extensions(
                self.cfg, self.tc, "linux-x64", self.build, self.dist, str(self.repo)
            )
        self.assertEqual(skipped, ["foo"])

    def test_download_failure_falls_back(self):
        """官方产物下载失败（DownloadError）→ 本地打包兜底。"""

        def _fake_download(url, dest, **kw):
            raise download_mod.DownloadError("s3 gone")

        with mock.patch.object(
            download_mod, "get_json", return_value={"data": [{"version": "1.2.3", "schema_version": 1}]}
        ), mock.patch.object(download_mod, "download_file", side_effect=_fake_download), \
             mock.patch.object(ext_mod, "_init_submodule", return_value=True) as sub, \
             mock.patch.object(ext_mod, "_package_extension", return_value=True), \
             mock.patch.object(ext_mod, "_place_extension", return_value=True):
            skipped = ext_mod.build_extensions(
                self.cfg, self.tc, "linux-x64", self.build, self.dist, str(self.repo)
            )
        self.assertEqual(skipped, [])
        sub.assert_called_once()

    def test_broken_api_artifact_no_fallback(self):
        """官方产物缺 wasm → 跳过且不走兜底（上游异常），并清理 installed。"""
        _ext_dir(self.dist, "foo")
        (self.dist / "data" / "extensions" / "installed" / "foo" / "extension.toml").write_text(
            "name = 'foo'\nversion = '0.9.0'\n"
        )

        def _fake_download(url, dest, **kw):
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(b"tar")

        def _fake_extract(archive, dest_dir):
            (Path(dest_dir) / "extension.toml").parent.mkdir(parents=True, exist_ok=True)
            (Path(dest_dir) / "extension.toml").write_text("name = 'foo'\nversion = '1.2.3'\n")

        with mock.patch.object(
            download_mod, "get_json", return_value={"data": [{"version": "1.2.3", "schema_version": 1}]}
        ), mock.patch.object(download_mod, "download_file", side_effect=_fake_download), \
             mock.patch.object(download_mod, "extract_archive", side_effect=_fake_extract), \
             mock.patch.object(ext_mod, "_init_submodule") as sub:
            skipped = ext_mod.build_extensions(
                self.cfg, self.tc, "linux-x64", self.build, self.dist, "/tmp/some-repo"
            )
        self.assertEqual(skipped, ["foo"])
        sub.assert_not_called()
        self.assertFalse(
            (self.dist / "data" / "extensions" / "installed" / "foo").exists(),
            "校验失败应清理 installed 防坏扩展",
        )

    def test_no_compatible_version_no_fallback(self):
        """无兼容版本 → 跳过且不走兜底。"""
        with mock.patch.object(
            download_mod, "get_json",
            return_value={"data": [{"version": "2.0.0", "schema_version": 2}]},
        ), mock.patch.object(ext_mod, "_init_submodule") as sub:
            skipped = ext_mod.build_extensions(
                self.cfg, self.tc, "linux-x64", self.build, self.dist, "/tmp/some-repo"
            )
        self.assertEqual(skipped, ["foo"])
        sub.assert_not_called()

    def test_missing_repo_fallback_skips(self):
        """元数据失败且兜底仓库不存在 → 跳过（不 raise）。"""
        with mock.patch.object(download_mod, "get_json", side_effect=download_mod.DownloadError("net")):
            skipped = ext_mod.build_extensions(
                self.cfg, self.tc, "linux-x64", self.build, self.dist,
                "/nonexistent/extensions-repo",
            )
        self.assertEqual(skipped, ["foo"])

    def test_empty_ids_skip(self):
        """无扩展配置 → 返回 [] 且不触碰任何下载。"""
        with mock.patch.object(download_mod, "get_json") as gj:
            skipped = ext_mod.build_extensions(
                {}, self.tc, "linux-x64", self.build, self.dist, "/tmp/some-repo"
            )
        self.assertEqual(skipped, [])
        gj.assert_not_called()


class LazyToolchainBackfillTests(unittest.TestCase):
    """_package_extension 兜底：tc 字段 None → ensure_wasi_sdk/ensure_zed_extension_cli 惰性回填。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="ext_lazy_"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.repo = self._tmp / "repo"
        (self.repo / "extensions" / "foo").mkdir(parents=True)
        self.wasi = self._tmp / "wasi-sdk-25.0-x86_64-linux"
        self.wasi.mkdir()
        self.cli = self._tmp / "zed-extension"
        self.cli.write_text("#!/bin/sh\nexit 0\n")
        self.cli.chmod(0o755)
        self.tc = _tc()
        self.out = self._tmp / "out"
        self.scratch = self._tmp / "scratch"

    def test_backfills_when_none(self):
        """tc.wasi_sdk_path/zed_extension 为 None → 回填并成功打包。"""
        env_captured = {}

        def _fake_run(cmd, **kw):
            env_captured["env"] = kw.get("env", {})
            # 模拟 zed-extension CLI 产出（真实 CLI 会生成 extension.toml + wasm）
            self.out.mkdir(parents=True, exist_ok=True)
            (self.out / "extension.toml").write_text("name = 'foo'\nversion = '1.0.0'\n")
            (self.out / "extension.wasm").write_bytes(b"\0asm")
            return FakeProc()

        with mock.patch.object(toolchain_mod, "ensure_wasi_sdk", return_value=self.wasi), \
             mock.patch.object(toolchain_mod, "ensure_zed_extension_cli", return_value=self.cli), \
             mock.patch.object(ext_mod.subprocess, "run", side_effect=_fake_run):
            ok = ext_mod._package_extension(
                self.tc, "foo", self.repo, self.scratch, self.out,
                self._tmp / "build", "linux-x64",
            )
        self.assertTrue(ok)
        self.assertEqual(self.tc.wasi_sdk_path, self.wasi)      # 回填
        self.assertEqual(self.tc.zed_extension, self.cli)       # 回填
        self.assertEqual(env_captured["env"].get("WASI_SDK_PATH"), str(self.wasi))
        self.assertTrue((self.out / "extension.toml").is_file())

    def test_wasi_failure_skips_before_cli(self):
        """WASI SDK 惰性获取失败 → 返回 False，CLI 不回填、不打包。"""
        with mock.patch.object(
            toolchain_mod, "ensure_wasi_sdk", side_effect=RuntimeError("boom")
        ), mock.patch.object(toolchain_mod, "ensure_zed_extension_cli") as cli:
            ok = ext_mod._package_extension(
                self.tc, "foo", self.repo, self.scratch, self.out,
                self._tmp / "build", "linux-x64",
            )
        self.assertFalse(ok)
        self.assertIsNone(self.tc.wasi_sdk_path)
        self.assertIsNone(self.tc.zed_extension)
        cli.assert_not_called()

    def test_cli_failure_skips(self):
        """CLI 惰性获取失败 → 返回 False（WASI 已回填）。"""
        with mock.patch.object(toolchain_mod, "ensure_wasi_sdk", return_value=self.wasi), \
             mock.patch.object(
                 toolchain_mod, "ensure_zed_extension_cli", side_effect=RuntimeError("boom")
             ):
            ok = ext_mod._package_extension(
                self.tc, "foo", self.repo, self.scratch, self.out,
                self._tmp / "build", "linux-x64",
            )
        self.assertFalse(ok)
        self.assertEqual(self.tc.wasi_sdk_path, self.wasi)
        self.assertIsNone(self.tc.zed_extension)

    def test_preexisting_toolchain_not_refetched(self):
        """tc 字段已就绪（目录/可执行）→ 不再调用惰性获取函数。"""
        self.tc.wasi_sdk_path = self.wasi
        self.tc.zed_extension = self.cli

        def _fake_run(cmd, **kw):
            return FakeProc()

        with mock.patch.object(toolchain_mod, "ensure_wasi_sdk") as ws, \
             mock.patch.object(toolchain_mod, "ensure_zed_extension_cli") as cli, \
             mock.patch.object(ext_mod.subprocess, "run", side_effect=_fake_run):
            ok = ext_mod._package_extension(
                self.tc, "foo", self.repo, self.scratch, self.out,
                self._tmp / "build", "linux-x64",
            )
        self.assertTrue(ok)
        ws.assert_not_called()
        cli.assert_not_called()


class IsValidExtensionArtifactTests(unittest.TestCase):
    """is_valid_extension_artifact：extension.toml 必需 + 三种合法内容形态之一
    （根 extension.wasm / grammars/*.wasm / themes/*.json——纯主题扩展无任何 wasm）。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="ext_valid_"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _ext_dir(self, eid="ext", with_toml=True) -> Path:
        d = self._tmp / eid
        d.mkdir(parents=True, exist_ok=True)
        if with_toml:
            (d / "extension.toml").write_text("name = 'x'\nversion = '1.0.0'\n")
        return d

    def test_root_wasm_valid(self):
        d = self._ext_dir()
        (d / "extension.wasm").write_bytes(b"\0asm")
        self.assertTrue(ext_mod.is_valid_extension_artifact(d))

    def test_grammars_wasm_valid(self):
        d = self._ext_dir()
        (d / "grammars").mkdir()
        (d / "grammars" / "x.wasm").write_bytes(b"x")
        self.assertTrue(ext_mod.is_valid_extension_artifact(d))

    def test_theme_json_valid(self):
        """纯主题扩展（material-theme 等）：extension.toml + themes/*.json，无 wasm → 合法。"""
        d = self._ext_dir()
        (d / "themes").mkdir()
        (d / "themes" / "material-theme.json").write_text("{}")
        self.assertTrue(ext_mod.is_valid_extension_artifact(d))

    def test_icons_valid(self):
        """图标扩展（vscode-icons 等）：extension.toml + icons/*.svg（+ icon_themes/*.json）→ 合法。"""
        d = self._ext_dir()
        (d / "icons").mkdir()
        (d / "icons" / "haskell.svg").write_text("<svg/>")
        (d / "icon_themes").mkdir()
        (d / "icon_themes" / "theme.json").write_text("{}")
        self.assertTrue(ext_mod.is_valid_extension_artifact(d))

    def test_snippets_valid(self):
        """snippet 扩展（kubernetes-snippets 等）：extension.toml + snippets/*.json → 合法。"""
        d = self._ext_dir()
        (d / "snippets").mkdir()
        (d / "snippets" / "yaml.json").write_text("{}")
        self.assertTrue(ext_mod.is_valid_extension_artifact(d))

    def test_toml_only_invalid(self):
        """只有 extension.toml（无 wasm / grammars / themes / icons / snippets）→ 不合法。"""
        d = self._ext_dir()
        self.assertFalse(ext_mod.is_valid_extension_artifact(d))

    def test_missing_toml_invalid(self):
        d = self._ext_dir("ext2", with_toml=False)
        (d / "themes").mkdir(parents=True, exist_ok=True)
        (d / "themes" / "t.json").write_text("{}")
        self.assertFalse(ext_mod.is_valid_extension_artifact(d))


if __name__ == "__main__":
    unittest.main()
