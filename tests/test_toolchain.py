"""tests/test_toolchain.py — P1 惰性化后 toolchain 逻辑单测（L1，零网络，unittest 风格）。

覆盖：
- ensure_zed_binary：本地路径分支（不触发 WASI/CLI 下载，返回字段为 None）；
  download 分支幂等跳过（已存在且校验通过 → 不重下、不重新解析 tag）；
  download 分支解析 + 下载调用。
- WindowsInstallerTests：_run_zed_installer 静默参数与失败 raise、
  _collect_windows_install 布局复制（含缺失容错）、windows 下载分支端到端
  （安装器下载 → 静默安装 mock → 布局收集 → cli 版校验）。
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


class WindowsInstallerTests(unittest.TestCase):
    """windows 安装器路径：_run_zed_installer 静默参数 / _collect_windows_install 布局 /
    下载分支端到端（linux 上不能真执行 windows exe，安装与校验均 mock）。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="tc_win_"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.build = self._tmp / "build"
        self.dist = self._tmp / "dist"

    def _make_install_layout(self, install: Path, *, with_amd_ags=True, with_arm64=True) -> None:
        """按 zed.iss [Files] 布局造安装产物。"""
        install = Path(install)
        (install / "bin").mkdir(parents=True, exist_ok=True)
        (install / "Zed.exe").write_bytes(b"GUI")
        (install / "bin" / "zed.exe").write_bytes(b"CLI")
        (install / "conpty.dll").write_bytes(b"c")
        if with_amd_ags:
            (install / "amd_ags_x64.dll").write_bytes(b"a")
        (install / "x64").mkdir(parents=True, exist_ok=True)
        (install / "x64" / "OpenConsole.exe").write_bytes(b"o")
        if with_arm64:
            (install / "arm64").mkdir(parents=True, exist_ok=True)
            (install / "arm64" / "OpenConsole.exe").write_bytes(b"o64")

    def test_run_zed_installer_silent_args(self):
        """安装器以静默参数运行（/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR= /TASKS=，timeout 600）。"""
        installer = self._tmp / "Zed-x86_64.exe"
        install_dir = self._tmp / "install"
        proc = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(toolchain_mod.subprocess, "run", return_value=proc) as run:
            toolchain_mod._run_zed_installer(installer, install_dir)
        run.assert_called_once_with(
            [
                str(installer),
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/NORESTART",
                f"/DIR={install_dir}",
                "/TASKS=",
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )

    def test_run_zed_installer_nonzero_exit_raises(self):
        """安装器非零退出码 → RuntimeError（含退出码与 stdout/stderr 摘要）。"""
        installer = self._tmp / "Zed-x86_64.exe"
        proc = mock.Mock(returncode=5, stdout="boom", stderr="err")
        with mock.patch.object(toolchain_mod.subprocess, "run", return_value=proc):
            with self.assertRaises(RuntimeError) as ctx:
                toolchain_mod._run_zed_installer(installer, self._tmp / "install")
        msg = str(ctx.exception)
        self.assertIn("5", msg)
        self.assertIn("boom", msg)
        self.assertIn("err", msg)

    def test_collect_windows_install_layout(self):
        """安装产物按布局复制：Zed.exe→dist 根、bin/zed.exe→dist/bin、conpty/amd_ags→根、
        x64/arm64 OpenConsole→对应子目录。"""
        install = self._tmp / "install"
        self._make_install_layout(install)
        toolchain_mod._collect_windows_install(install, self.dist)
        expected = (
            "Zed.exe",
            "bin/zed.exe",
            "conpty.dll",
            "amd_ags_x64.dll",
            "x64/OpenConsole.exe",
            "arm64/OpenConsole.exe",
        )
        for rel in expected:
            self.assertTrue((self.dist / rel).is_file(), f"缺少 dist/{rel}")

    def test_collect_windows_install_missing_zed_raises(self):
        """安装目录缺 Zed.exe → RuntimeError。"""
        install = self._tmp / "install"
        install.mkdir(parents=True)
        with self.assertRaises(RuntimeError) as ctx:
            toolchain_mod._collect_windows_install(install, self.dist)
        self.assertIn("Zed.exe", str(ctx.exception))

    def test_collect_windows_install_tolerates_missing_optional(self):
        """aarch64 容错：amd_ags_x64.dll / arm64 OpenConsole 缺失不报错。"""
        install = self._tmp / "install"
        self._make_install_layout(install, with_amd_ags=False, with_arm64=False)
        toolchain_mod._collect_windows_install(install, self.dist)  # 不 raise
        self.assertTrue((self.dist / "Zed.exe").is_file())
        self.assertTrue((self.dist / "bin" / "zed.exe").is_file())
        self.assertFalse((self.dist / "amd_ags_x64.dll").exists())
        self.assertFalse((self.dist / "arm64").exists())

    def test_windows_download_branch_end_to_end(self):
        """windows 下载分支：下载安装器 → 静默安装（mock 造布局）→ 收集 → cli 版校验。"""
        urls = []

        def _fake_download(url, dest, **kw):
            urls.append(url)
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"MZ")  # 假安装器

        def _fake_install(installer, install_dir):
            self._make_install_layout(install_dir)  # 模拟静默安装产物

        with mock.patch.object(
            download_mod, "github_asset_url", return_value="https://example.invalid/Zed-x86_64.exe"
        ), mock.patch.object(download_mod, "download_file", side_effect=_fake_download), \
                mock.patch.object(toolchain_mod, "_run_zed_installer", side_effect=_fake_install), \
                mock.patch.object(toolchain_mod, "_verify_zed") as verify:
            toolchain_mod._download_zed("v1.0.0", "windows-x64", self.build, self.dist)

        # 安装器下载到 build 目录
        self.assertTrue((self.build / "Zed-x86_64.exe").is_file())
        # 布局：Zed.exe 在 dist 根、cli 在 dist/bin、差异文件容错
        self.assertTrue((self.dist / "Zed.exe").is_file())
        self.assertTrue((self.dist / "bin" / "zed.exe").is_file())
        self.assertTrue((self.dist / "conpty.dll").is_file())
        self.assertTrue((self.dist / "x64" / "OpenConsole.exe").is_file())
        # 校验必须用 cli 版 bin/zed.exe（GUI Zed.exe 无 stdout）
        verify.assert_called_once_with(self.dist / "bin" / "zed.exe")


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
        self.assertIn("rust compilation forbidden", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
