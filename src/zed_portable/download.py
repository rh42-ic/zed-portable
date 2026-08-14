"""下载助手：GitHub 资产下载 / 校验 / 解压（Lane B/C 公共契约）。

- 请求超时 15s；下载失败重试 3 次（指数退避）。
- 幂等下载：dest 已存在且校验通过则跳过。
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import tarfile
import time
import zipfile
from pathlib import Path

import requests

TIMEOUT = 15  # 单请求超时（秒）
RETRIES = 3  # 下载重试次数


class DownloadError(Exception):
    """下载或校验失败。"""


class AssetNotFoundError(DownloadError):
    """release 中找不到匹配资产。"""


def github_headers() -> dict:
    """构造 GitHub API 请求头；环境变量 GITHUB_TOKEN 存在时附加 Bearer。"""
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def get_json(url: str, headers: dict | None = None) -> dict:
    """GET 并解析 JSON；非 2xx 抛异常。"""
    resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def download_file(
    url: str,
    dest: Path,
    *,
    expected_sha256: str | None = None,
    headers: dict | None = None,
) -> Path:
    """流式下载到 dest.tmp 后原子 rename，返回 dest。

    幂等：dest 已存在且（无 expected_sha256 或 sha256 匹配）→ 直接返回。
    网络失败重试 RETRIES 次（指数退避）；校验失败立即抛 DownloadError。
    """
    dest = Path(dest)
    headers = dict(headers or {})
    if dest.exists():
        if expected_sha256 is None or sha256_of(dest) == expected_sha256:
            return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    last_err: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with requests.get(url, headers=headers, timeout=TIMEOUT, stream=True) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            fh.write(chunk)
            if expected_sha256 is not None and sha256_of(tmp) != expected_sha256:
                raise DownloadError(f"sha256 校验失败（期望 {expected_sha256}）: {url}")
            os.replace(tmp, dest)
            return dest
        except DownloadError:
            raise
        except Exception as exc:  # noqa: BLE001 网络层失败统一重试
            last_err = exc
            if attempt < RETRIES - 1:
                time.sleep(2**attempt)
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    raise DownloadError(f"下载失败（{RETRIES} 次重试后）: {url}") from last_err


def sha256_of(path: Path) -> str:
    """文件 sha256 十六进制字符串。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_archive(archive: Path, dest_dir: Path) -> None:
    """按后缀分发解压（纯解压；顶层单目录包装由调用方处理）。

    - .tar.gz / .tgz / .tar.xz → tarfile（r:* 自动识别 gzip/xz，lzma 为内置模块）
    - .zip → zipfile
    - 裸 .gz → gzip 解压为去 .gz 后缀的同名文件
    """
    archive = Path(archive)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = archive.name
    if name.endswith(".tar.gz") or name.endswith(".tgz") or name.endswith(".tar.xz"):
        with tarfile.open(archive, "r:*") as tf:
            tf.extractall(dest_dir)
    elif name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest_dir)
    elif name.endswith(".gz"):
        out_name = name[: -len(".gz")]
        with gzip.open(archive, "rb") as fin, open(dest_dir / out_name, "wb") as fout:
            shutil.copyfileobj(fin, fout)
    else:
        raise DownloadError(f"不支持的归档格式: {name}")


def github_asset_url(
    repo: str,
    tag: str,
    candidates: list[str],
    *,
    headers: dict | None = None,
) -> str:
    """在 GitHub release 中定位资产下载 URL。

    1. 对每个候选名 HEAD `https://github.com/{repo}/releases/download/{tag}/{name}`，
       200 → 直接返回该 URL；
    2. 全部未命中 → GET `https://github.com/{repo}/releases/expanded_assets/{tag}`
       抓 HTML 解析真实资产名，与任一候选名模糊匹配（字面包含；{tag}/{version}
       占位符按通配）命中后返回对应 URL；
    3. 仍无 → raise AssetNotFoundError。
    """
    headers = dict(headers or {})
    for cand in candidates:
        url = f"https://github.com/{repo}/releases/download/{tag}/{cand}"
        try:
            resp = requests.head(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
            if resp.status_code == 200:
                return url
        except requests.RequestException:
            continue
    asset_names = _expanded_asset_names(repo, tag, headers)
    for cand in candidates:
        for asset_name in asset_names:
            if _asset_matches(cand, asset_name):
                return f"https://github.com/{repo}/releases/download/{tag}/{asset_name}"
    raise AssetNotFoundError(
        f"release 中未找到资产: {repo} @ {tag}（候选: {candidates}）"
    )


def _expanded_asset_names(repo: str, tag: str, headers: dict) -> list[str]:
    """解析 expanded_assets 页 HTML 中的资产名（去重保序）。"""
    url = f"https://github.com/{repo}/releases/expanded_assets/{tag}"
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        return []
    pattern = re.compile(
        r'href="[^"]*/releases/download/' + re.escape(tag) + r'/([^"/]+)"'
    )
    return list(dict.fromkeys(pattern.findall(resp.text)))


def _asset_matches(candidate: str, asset_name: str) -> bool:
    """候选名与资产名模糊匹配。"""
    if "{tag}" in candidate or "{version}" in candidate:
        pattern = (
            re.escape(candidate)
            .replace(re.escape("{tag}"), ".*")
            .replace(re.escape("{version}"), ".*")
        )
        return re.fullmatch(pattern, asset_name) is not None
    return candidate in asset_name
