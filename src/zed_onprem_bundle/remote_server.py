"""P2.5 远程服务端：预置全部所需平台 zed-remote-server 资产（§5 P2.5）。

Zed 远程开发时客户端把 zed-remote-server 部署到远程机器，远程平台由远程
探测决定（与客户端平台无关）。客户端本地缓存路径
{data_dir}/remote_servers/{channel}/{os}-{arch}/{version}.gz，只做 metadata
存在性检查即跳过下载——因此本阶段按原样改名落位（零转换），使任意平台
远程（含离线）连接时客户端零下载直接部署。server 版本 = 客户端版本；
zed.binary 为本地路径时版本未知 → 跳过。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import SUPPORTED_PLATFORMS
from .download import DownloadError, download_file

log = logging.getLogger("zed_onprem_bundle.remote_server")

REMOTE_SERVER_REPO = "zed-industries/zed"

#: gzip 魔数（linux/macos 资产）
_GZIP_MAGIC = b"\x1f\x8b"
#: zip 魔数（windows 资产）
_ZIP_MAGIC = b"PK"


def ensure_remote_servers(cfg: dict, zed_tag: str, data_dir: Path) -> list[str]:
    """按 cfg remote_server.platforms 下载全部平台 zed-remote-server 资产，
    原样改名落位 {data_dir}/remote_servers/{channel}/{os}-{arch}/{version}.gz。

    - 未配置 remote_server（无键或 platforms 空）→ 未启用，log.info 跳过，返回 []
    - zed_tag == "local"（cfg zed.binary 为本地路径）→ 版本未知，log.warning 跳过，返回 []
    - 幂等：dest 存在且魔数正确 → 跳过
    - 下载失败/魔数不符 → raise（远程服务端是 zed 核心功能，硬失败）
    返回本次确认的平台列表。
    """
    if not cfg.get("remote_server") or not cfg.get("remote_server", {}).get("platforms"):
        log.info("未配置 remote_server（链接 config/available/remote.toml 启用），跳过")
        return []
    if zed_tag == "local":
        log.warning("zed.binary 为本地路径，remote server 版本未知，跳过预置")
        return []
    channel = (cfg.get("zed") or {}).get("channel", "stable")
    version = zed_tag.removeprefix("v")
    confirmed: list[str] = []
    for platform in cfg["remote_server"]["platforms"]:
        os_name, arch = platform.split("-", 1)
        dest = data_dir / "remote_servers" / channel / platform / f"{version}.gz"
        if dest.exists() and _magic_ok(dest, os_name):
            log.info("remote server 已存在，跳过: %s", dest)
            confirmed.append(platform)
            continue
        suffix = "zip" if os_name == "windows" else "gz"
        url = (
            f"https://github.com/{REMOTE_SERVER_REPO}/releases/download/{zed_tag}/"
            f"zed-remote-server-{platform}.{suffix}"
        )
        download_file(url, dest)  # 内部自动 mkdir 父目录、重试 3 次、原子 rename
        if not _magic_ok(dest, os_name):
            raise DownloadError(f"remote server 资产魔数校验失败: {dest}")
        log.info("remote server 落位: %s ← %s", dest, url)
        confirmed.append(platform)
    return confirmed


def _magic_ok(path: Path, os_name: str) -> bool:
    """魔数校验（读前 2 字节）：windows → PK（zip）；其他 → 1f8b（gzip）。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(2)
    except OSError:
        return False
    expected = _ZIP_MAGIC if os_name == "windows" else _GZIP_MAGIC
    return head == expected
