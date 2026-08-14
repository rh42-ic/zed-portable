"""tests/test_toolchain.py — P1 惰性化后 toolchain 逻辑单测（L1，零网络，unittest 风格）。

覆盖：
- ensure_zed_binary：本地路径分支（不触发 WASI/CLI 下载，返回字段为 None）；
  download 分支幂等跳过（已存在且校验通过 → 不重下、不重新解析 tag）；
  download 分支解析 + 下载调用。
- ensure_wasi_sdk / ensure_zed_extension_cli：幂等（已存在直接返回，零下载）；
  ensure_zed_extension_cli 下载路径（预编译 URL 模板含 commit sha）。

运行：uv run pytest tests/test_toolchain.py -q（或全量 uv run pytest -q）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zed_portable import download as download_mod
from zed_portable import toolchain as toolchain_mod


def _write_executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


class EnsureZedBinaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="tc_bin_"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.build = self._tmp / "build"
        self.dist = self._tmp / "dist"

    def test_local_binary_branch_no_toolchain_download(self):
        """binary=路径 → 复制本地 zed；wasi/zed_extension 保持 None，不触发任何工具链下载。"""
        fake = _write_executable(self._tmp / "my-zed", "#!/bin/sh\nprintf 'Zed 1.0.0'\nexit 0\n")
        cfg = {"zed": {"binary": str(fake)}}
        with mock.patch.object(download_mod, "download_file") as dl:
            tc = toolchain_mod.ensure_zed_binary(cfg, "linux-x64", self.build, self.dist)
        self.assertIsNone(tc.wasi_sdk_path)        # 惰性：None
        self.assertIsNone(tc.zed_extension)        # 惰性：None
        self.assertEqual(tc.zed_tag, "local")
        self.assertEqual(tc.zed_commit, "")
        self.assertTrue(tc.zed_bin.is_file())
        self.assertTrue(os.access(tc.zed_bin, os.X_OK))
        dl.assert_not_called()

    def test_download_idempotent_skip(self):
        """已存在且 --version 通过 → 跳过下载与 tag 解析（release_tag 非空直接记录）。"""
        fake = _write_executable(
            self.dist / "bin" / "zed",
            "#!/bin/sh\nprintf 'Zed 0.180.0'\nexit 0\n",
        )
        cfg = {"zed": {"binary": "download", "release_tag": "v0.180.0"}}
        with mock.patch.object(toolchain_mod, "resolve_zed_tag") as resolve, \
             mock.patch.object(toolchain_mod, "_download_zed") as dl:
            tc = toolchain_mod.ensure_zed_binary(cfg, "linux-x64", self.build, self.dist)
        self.assertEqual(tc.zed_tag, "v0.180.0")
        self.assertEqual(tc.zed_commit, "")
        resolve.assert_not_called()
        dl.assert_not_called()

    def test_download_branch_resolves_and_downloads(self):
        """未安装 → resolve tag + 下载调用（linux 资产）；工具链字段仍为 None。"""
        cfg = {"zed": {"binary": "download"}}
        with mock.patch.object(
            toolchain_mod, "resolve_zed_tag", return_value=("v1.15.0", "e17dc4f9")
        ) as resolve, mock.patch.object(toolchain_mod, "_download_zed") as dl:
            tc = toolchain_mod.ensure_zed_binary(cfg, "linux-x64", self.build, self.dist)
        self.assertEqual(tc.zed_tag, "v1.15.0")
        self.assertEqual(tc.zed_commit, "e17dc4f9")
        self.assertIsNone(tc.wasi_sdk_path)
        self.assertIsNone(tc.zed_extension)
        resolve.assert_called_once()
        dl.assert_called_once_with("v1.15.0", "linux-x64", self.build, self.dist)

    def test_local_branch_windows_exe_suffix(self):
        fake = _write_executable(self._tmp / "my-zed.exe", "#!/bin/sh\nexit 0\n")
        cfg = {"zed": {"binary": str(fake)}}
        tc = toolchain_mod.ensure_zed_binary(cfg, "windows-x64", self.build, self.dist)
        self.assertTrue(tc.zed_bin.name == "zed.exe")
        self.assertTrue(tc.zed_bin.is_file())


class EnsureWasiSdkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="tc_wasi_"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.build = self._tmp / "build"

    def test_idempotent_returns_inner_dir(self):
        """build/wasi-sdk 已含 wasi-sdk-25.0-* 内部目录 → 直接返回，零下载。"""
        inner = self.build / "wasi-sdk" / "wasi-sdk-25.0-x86_64-linux"
        inner.mkdir(parents=True)
        with mock.patch.object(download_mod, "download_file", side_effect=AssertionError("不应下载")):
            result = toolchain_mod.ensure_wasi_sdk(self.build, "linux-x64")
        self.assertEqual(result, inner)

    def test_missing_inner_dir_raises(self):
        """wasi-sdk 目录存在但无内部目录 → raise（网络阶段被 mock 成失败）。"""
        (self.build / "wasi-sdk").mkdir(parents=True)
        with mock.patch.object(download_mod, "download_file", side_effect=download_mod.DownloadError("offline")):
            with self.assertRaises(RuntimeError):
                toolchain_mod.ensure_wasi_sdk(self.build, "linux-x64")

    def test_windows_asset_name(self):
        """windows 平台 → 下载 URL 使用 -windows 资产名（wasi-sdk 不存在 → 走下载路径）。"""
        inner = self.build / "wasi-sdk" / "wasi-sdk-25.0-x86_64-windows"
        urls = []

        def _fake_download(url, dest, **kw):
            urls.append(url)
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            inner.mkdir(parents=True)

        with mock.patch.object(download_mod, "download_file", side_effect=_fake_download), \
             mock.patch.object(download_mod, "extract_archive"):
            result = toolchain_mod.ensure_wasi_sdk(self.build, "windows-x64")
        self.assertEqual(result, inner)
        self.assertTrue(urls[0].endswith("/wasi-sdk-25.0-x86_64-windows.tar.gz"))


class EnsureZedExtensionCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="tc_cli_"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.build = self._tmp / "build"

    def test_idempotent_returns_existing(self):
        """已存在且可执行 → 直接返回，零下载。"""
        cli = _write_executable(self.build / "zed-extension")
        with mock.patch.object(download_mod, "download_file", side_effect=AssertionError("不应下载")):
            result = toolchain_mod.ensure_zed_extension_cli(self.build, "linux-x64", "deadbeef")
        self.assertEqual(result, cli)

    def test_downloads_prebuilt_cli(self):
        """不存在 → 下载预编译 CLI（URL 含 commit sha 与平台路径），linux chmod 可执行。"""
        urls = []

        def _fake_download(url, dest, **kw):
            urls.append(url)
            _write_executable(Path(dest), "#!/bin/sh\nexit 0\n")

        with mock.patch.object(download_mod, "download_file", side_effect=_fake_download), \
             mock.patch.object(toolchain_mod, "_extension_cli_sha", return_value="0123456789abcdef"):
            result = toolchain_mod.ensure_zed_extension_cli(self.build, "linux-x64", "deadbeef")
        self.assertEqual(result, self.build / "zed-extension")
        self.assertTrue(os.access(result, os.X_OK))
        self.assertIn("0123456789abcdef/x86_64-unknown-linux-gnu/zed-extension", urls[0])

    def test_sha_empty_falls_back_to_commit(self):
        """_extension_cli_sha 取不到 → 用 zed_commit 拼 URL。"""
        urls = []

        def _fake_download(url, dest, **kw):
            urls.append(url)
            _write_executable(Path(dest), "#!/bin/sh\nexit 0\n")

        with mock.patch.object(download_mod, "download_file", side_effect=_fake_download), \
             mock.patch.object(toolchain_mod, "_extension_cli_sha", return_value=""):
            toolchain_mod.ensure_zed_extension_cli(self.build, "linux-x64", "deadbeef")
        self.assertIn("deadbeef/x86_64-unknown-linux-gnu/zed-extension", urls[0])

    def test_download_failure_raises_with_ci_hint(self):
        with mock.patch.object(download_mod, "download_file", side_effect=download_mod.DownloadError("offline")):
            with self.assertRaises(RuntimeError) as ctx:
                toolchain_mod.ensure_zed_extension_cli(self.build, "linux-x64", "deadbeef")
        self.assertIn("cargo build -p extension_cli", str(ctx.exception))
        self.assertIn("本机禁 rust 编译", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
