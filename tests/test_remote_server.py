"""tests/test_remote_server.py — P2.5 remote_server 纯逻辑单测（L1，零网络，unittest 风格）。

覆盖 ensure_remote_servers：全平台下载（URL/落位路径）、幂等跳过、魔数不符
硬失败、local zed_tag 跳过、自定义平台+channel。不联网、快速（<30s）。

运行：`uv run python -m unittest discover -s tests -v`（workdir=项目根）。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 包在 src/ 布局：unittest discover 从项目根跑时，tests 内直接 import
# zed_portable 会失败——此处把项目根/src 插入 sys.path（与 test_finalize 同方案）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zed_portable import remote_server as remote_server_mod
from zed_portable.download import DownloadError

#: 默认 6 平台（与 config.SUPPORTED_PLATFORMS 一致）
DEFAULT_PLATFORMS = [
    "linux-x86_64",
    "linux-aarch64",
    "macos-x86_64",
    "macos-aarch64",
    "windows-x86_64",
    "windows-aarch64",
]


def _make_cfg(platforms=None, channel="stable"):
    """构造最小 cfg（remote_server.platforms + zed.channel）。"""
    return {
        "zed": {"channel": channel},
        "remote_server": {"platforms": list(platforms or DEFAULT_PLATFORMS)},
    }


def _fake_download(calls):
    """构造 download_file 的假实现：记录 (url, dest)，写平台对应魔数内容。"""
    def _impl(url, dest, **kwargs):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        magic = b"PK" if url.endswith(".zip") else b"\x1f\x8b"
        dest.write_bytes(magic + b"\x00" * 8)
        calls.append((url, dest))
    return _impl


def _expected_dest(data_dir, channel, platform, version):
    """期望落位路径：{data_dir}/remote_servers/{channel}/{platform}/{version}.gz。"""
    return data_dir / "remote_servers" / channel / platform / f"{version}.gz"


class EnsureRemoteServersTests(unittest.TestCase):
    """ensure_remote_servers：下载/落位/URL 与平台对应关系。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="remote_server_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self._tmp, ignore_errors=True))
        self.data_dir = self._tmp / "data"

    def test_all_platforms_download(self):
        """默认 6 平台 + stable：6 次下载；dest=v 前缀剥离后的 {version}.gz；
        windows URL 含 .zip、其他含 .gz。"""
        calls = []
        cfg = _make_cfg()
        with mock.patch.object(
            remote_server_mod, "download_file", side_effect=_fake_download(calls)
        ):
            result = remote_server_mod.ensure_remote_servers(cfg, "v1.15.0", self.data_dir)

        self.assertEqual(result, DEFAULT_PLATFORMS)
        self.assertEqual(len(calls), 6)
        for platform, (url, dest) in zip(DEFAULT_PLATFORMS, calls):
            self.assertEqual(dest, _expected_dest(self.data_dir, "stable", platform, "1.15.0"))
            suffix = ".zip" if platform.startswith("windows") else ".gz"
            self.assertTrue(url.endswith(suffix), f"{platform} URL 后缀应为 {suffix}: {url}")
            self.assertIn("/releases/download/v1.15.0/", url)

    def test_idempotent_skip(self):
        """全部 dest 已存在且魔数正确 → download_file 不被调用，仍返回 6 平台。"""
        for platform in DEFAULT_PLATFORMS:
            dest = _expected_dest(self.data_dir, "stable", platform, "1.15.0")
            dest.parent.mkdir(parents=True, exist_ok=True)
            magic = b"PK" if platform.startswith("windows") else b"\x1f\x8b"
            dest.write_bytes(magic + b"\x00" * 8)
        calls = []
        with mock.patch.object(
            remote_server_mod, "download_file", side_effect=_fake_download(calls)
        ):
            result = remote_server_mod.ensure_remote_servers(
                _make_cfg(), "v1.15.0", self.data_dir
            )
        self.assertEqual(result, DEFAULT_PLATFORMS)
        self.assertEqual(calls, [], "全部命中缓存，不应有任何下载调用")

    def test_bad_magic_raises(self):
        """下载后魔数不符（内容 XXXX）→ raise DownloadError（硬失败语义）。"""
        def _bad_download(url, dest, **kwargs):
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(b"XXXX")
        with mock.patch.object(remote_server_mod, "download_file", side_effect=_bad_download):
            with self.assertRaises(DownloadError) as ctx:
                remote_server_mod.ensure_remote_servers(_make_cfg(), "v1.15.0", self.data_dir)
        self.assertIn("魔数校验失败", str(ctx.exception))

    def test_local_zed_tag_skips(self):
        """zed_tag == 'local'（本地二进制，版本未知）→ 返回 [] 且零下载。"""
        calls = []
        with mock.patch.object(
            remote_server_mod, "download_file", side_effect=_fake_download(calls)
        ):
            result = remote_server_mod.ensure_remote_servers(_make_cfg(), "local", self.data_dir)
        self.assertEqual(result, [])
        self.assertEqual(calls, [], "local tag 不应触发任何下载")

    def test_missing_remote_server_skips(self):
        """cfg 无 remote_server 键（未链接 config/available/remote.toml）→
        返回 [] 且 download_file 不被调用（不链接 = 不下载）。"""
        calls = []
        cfg = {"zed": {"channel": "stable"}}  # 无 remote_server 键
        with mock.patch.object(
            remote_server_mod, "download_file", side_effect=_fake_download(calls)
        ):
            result = remote_server_mod.ensure_remote_servers(cfg, "v1.15.0", self.data_dir)
        self.assertEqual(result, [])
        self.assertEqual(calls, [], "未配置 remote_server 不应触发任何下载")

    def test_custom_platforms_and_channel(self):
        """自定义 2 平台 + preview channel：只下载 2 个、落位 remote_servers/preview/。"""
        platforms = ["linux-x86_64", "windows-x86_64"]
        calls = []
        with mock.patch.object(
            remote_server_mod, "download_file", side_effect=_fake_download(calls)
        ):
            result = remote_server_mod.ensure_remote_servers(
                _make_cfg(platforms, channel="preview"), "v1.15.0", self.data_dir
            )
        self.assertEqual(result, platforms)
        self.assertEqual(len(calls), 2)
        for platform, (url, dest) in zip(platforms, calls):
            self.assertEqual(dest, _expected_dest(self.data_dir, "preview", platform, "1.15.0"))
            suffix = ".zip" if platform.startswith("windows") else ".gz"
            self.assertTrue(url.endswith(suffix), f"{platform} URL 后缀应为 {suffix}: {url}")


if __name__ == "__main__":
    unittest.main()
