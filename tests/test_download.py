"""download.py 测试（无网络）：下载幂等 / 重试 / sha256 / 解压 / 请求头。

requests 不支持 file://（InvalidSchema），故用 unittest.mock 伪造 requests.get/
head 返回流式响应——零网络、秒级完成；重试计数恰好需要 mock。
运行：uv run python -m unittest discover -s tests -v
"""

from __future__ import annotations

import gzip
import io
import os
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import requests

from zed_onprem_bundle import download

SHA256_HELLO = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


class FakeResponse:
    """requests.Response 的最小模拟：上下文管理器 + 流式迭代 + json/text。"""

    def __init__(self, content: bytes = b"", status_code: int = 200,
                 json_data=None, text: str = ""):
        self.content = content
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def iter_content(self, chunk_size: int = 1024):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def json(self):
        return self._json

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Sha256OfTest(unittest.TestCase):
    def test_known_value(self):
        """sha256_of 与已知哈希常量一致（外部校验，非循环引用）。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "f.txt"
            p.write_bytes(b"hello")
            self.assertEqual(download.sha256_of(p), SHA256_HELLO)


class DownloadFileTest(unittest.TestCase):
    def test_download_writes_file(self):
        """正常下载：内容落盘、返回 dest、无残留 .tmp。"""
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "a.bin"
            with mock.patch("zed_onprem_bundle.download.requests.get",
                            return_value=FakeResponse(content=b"data-123")):
                ret = download.download_file("https://example.invalid/a.bin", dest)
            self.assertEqual(ret, dest)
            self.assertEqual(dest.read_bytes(), b"data-123")
            self.assertFalse(dest.with_name(dest.name + ".tmp").exists())

    def test_idempotent_second_call_no_request(self):
        """幂等：dest 已存在（无 expected_sha256）→ 第二次调用短路，不重新请求。"""
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "a.bin"
            calls = {"n": 0}

            def _get(url, **kw):
                calls["n"] += 1
                return FakeResponse(content=b"data-123")

            with mock.patch("zed_onprem_bundle.download.requests.get", side_effect=_get):
                download.download_file("https://example.invalid/a.bin", dest)
                download.download_file("https://example.invalid/a.bin", dest)
            self.assertEqual(calls["n"], 1)  # 第二次未发请求
            self.assertEqual(dest.read_bytes(), b"data-123")

    def test_expected_sha256_match_skips_redownload(self):
        """expected_sha256 匹配 → 跳过；两次调用只请求一次。"""
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "a.bin"
            calls = {"n": 0}

            def _get(url, **kw):
                calls["n"] += 1
                return FakeResponse(content=b"hello")

            with mock.patch("zed_onprem_bundle.download.requests.get", side_effect=_get):
                download.download_file("https://e.invalid/a", dest, expected_sha256=SHA256_HELLO)
                download.download_file("https://e.invalid/a", dest, expected_sha256=SHA256_HELLO)
            self.assertEqual(calls["n"], 1)

    def test_sha256_mismatch_raises_fast(self):
        """校验失败 → 立即 DownloadError（不重试，一次请求即止）。"""
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "a.bin"
            with mock.patch("zed_onprem_bundle.download.requests.get",
                            return_value=FakeResponse(content=b"hello")) as m:
                with self.assertRaises(download.DownloadError):
                    download.download_file("https://e.invalid/a", dest,
                                           expected_sha256="0" * 64)
            self.assertEqual(m.call_count, 1)

    def test_retries_then_download_error(self):
        """网络失败 → 重试 RETRIES 次（指数退避 1s/2s）→ DownloadError。"""
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "a.bin"
            sleeps = []

            def _get(url, **kw):
                raise requests.ConnectionError("network down")

            with mock.patch("zed_onprem_bundle.download.requests.get", side_effect=_get) as m, \
                 mock.patch("zed_onprem_bundle.download.time.sleep",
                            side_effect=lambda s: sleeps.append(s)):
                with self.assertRaises(download.DownloadError):
                    download.download_file("https://e.invalid/a", dest)
            self.assertEqual(m.call_count, download.RETRIES)  # 尝试 3 次
            self.assertEqual(sleeps, [1, 2])                  # 指数退避
            self.assertFalse(dest.exists())
            self.assertFalse(dest.with_name(dest.name + ".tmp").exists())  # 清理 tmp


class GetJsonTest(unittest.TestCase):
    def test_returns_json_dict(self):
        """get_json 返回解析后的 dict；非 2xx 抛 HTTPError。"""
        with mock.patch("zed_onprem_bundle.download.requests.get",
                        return_value=FakeResponse(json_data={"a": 1})):
            self.assertEqual(download.get_json("https://api.example.invalid/x"), {"a": 1})
        with mock.patch("zed_onprem_bundle.download.requests.get",
                        return_value=FakeResponse(status_code=404)):
            with self.assertRaises(requests.HTTPError):
                download.get_json("https://api.example.invalid/x")


class ExtractArchiveTest(unittest.TestCase):
    def _write_targz(self, path: Path, entries: dict[str, bytes]) -> None:
        with tarfile.open(path, "w:gz") as tf:
            for name, content in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))

    def test_tar_gz(self):
        """.tar.gz 解压内容正确。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            arc = td / "x.tar.gz"
            self._write_targz(arc, {"inner/a.txt": b"tar-a", "inner/b.txt": b"tar-b"})
            out = td / "out"
            download.extract_archive(arc, out)
            self.assertEqual((out / "inner/a.txt").read_bytes(), b"tar-a")
            self.assertEqual((out / "inner/b.txt").read_bytes(), b"tar-b")

    def test_tgz_alias(self):
        """.tgz 与 .tar.gz 同路径解压。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            arc = td / "x.tgz"
            self._write_targz(arc, {"f.txt": b"alias-ok"})
            out = td / "out"
            download.extract_archive(arc, out)
            self.assertEqual((out / "f.txt").read_bytes(), b"alias-ok")

    def test_zip(self):
        """.zip 解压内容正确。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            arc = td / "x.zip"
            with zipfile.ZipFile(arc, "w") as zf:
                zf.writestr("dir/c.txt", "zip-c")
            out = td / "out"
            download.extract_archive(arc, out)
            self.assertEqual((out / "dir/c.txt").read_text(), "zip-c")

    def test_bare_gz(self):
        """裸 .gz → 解压为去 .gz 后缀的同名文件。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            arc = td / "x.gz"
            with gzip.open(arc, "wb") as f:
                f.write(b"raw-gz-content")
            out = td / "out"
            download.extract_archive(arc, out)
            self.assertEqual((out / "x").read_bytes(), b"raw-gz-content")

    def test_unsupported_extension(self):
        """未知后缀 → DownloadError。"""
        with tempfile.TemporaryDirectory() as td:
            arc = Path(td) / "x.rar"
            arc.write_bytes(b"nope")
            with self.assertRaises(download.DownloadError):
                download.extract_archive(arc, Path(td) / "out")


class GithubHeadersTest(unittest.TestCase):
    def test_no_token_empty(self):
        """无 GITHUB_TOKEN env → {}。"""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(download.github_headers(), {})

    def test_with_token_bearer(self):
        """注入 GITHUB_TOKEN → {"Authorization": "Bearer <token>"}。"""
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_abc"}, clear=True):
            self.assertEqual(download.github_headers(),
                             {"Authorization": "Bearer ghp_abc"})


class GithubAssetUrlTest(unittest.TestCase):
    def test_head_hit_returns_url(self):
        """候选 HEAD 200 → 直接返回对应下载 URL（只请求一次）。"""
        with mock.patch("zed_onprem_bundle.download.requests.head",
                        return_value=FakeResponse(status_code=200)) as m:
            url = download.github_asset_url("o/r", "v1", ["zed-linux-x86_64.tar.gz"])
        self.assertEqual(url, "https://github.com/o/r/releases/download/v1/zed-linux-x86_64.tar.gz")
        self.assertEqual(m.call_count, 1)

    def test_html_fallback_fuzzy_match(self):
        """全 404 → GET expanded_assets HTML 模糊匹配资产名。"""
        html = ('<a href="/o/r/releases/download/v1/zed-linux-x86_64.tar.gz">z</a>'
                '<a href="/o/r/releases/download/v1/zed-linux-aarch64.tar.gz">a</a>')

        def _head(url, **kw):
            return FakeResponse(status_code=404)

        def _get(url, **kw):
            self.assertIn("expanded_assets", url)
            return FakeResponse(text=html)

        with mock.patch("zed_onprem_bundle.download.requests.head", side_effect=_head), \
             mock.patch("zed_onprem_bundle.download.requests.get", side_effect=_get):
            url = download.github_asset_url("o/r", "v1", ["zed-linux-x86_64.tar.gz"])
        self.assertEqual(url, "https://github.com/o/r/releases/download/v1/zed-linux-x86_64.tar.gz")

    def test_not_found_raises(self):
        """HEAD 全 404 且 HTML 无匹配 → AssetNotFoundError。"""
        with mock.patch("zed_onprem_bundle.download.requests.head",
                        return_value=FakeResponse(status_code=404)), \
             mock.patch("zed_onprem_bundle.download.requests.get",
                        return_value=FakeResponse(text="no assets")):
            with self.assertRaises(download.AssetNotFoundError):
                download.github_asset_url("o/r", "v1", ["nope-{tag}.tar.gz"])

    def test_asset_matches_placeholder(self):
        """占位符模糊匹配：{tag}/{version} 按通配匹配；字面包含匹配。"""
        self.assertTrue(download._asset_matches("zed-{version}-linux.tar.gz",
                                                "zed-0.180.0-linux.tar.gz"))
        self.assertTrue(download._asset_matches("zed", "zed-linux-x86_64.tar.gz"))
        self.assertFalse(download._asset_matches("zed", "svelte.tar.gz"))
        # {tag} 通配可跨越中间段（"0.1.tar"），但前缀必须匹配
        self.assertTrue(download._asset_matches("zed-{tag}.gz", "zed-0.1.tar.gz"))
        self.assertFalse(download._asset_matches("zed-{tag}.gz", "other-0.1.gz"))


if __name__ == "__main__":
    unittest.main()
