"""P3 Node 运行时：下载 → 解压 → 结构自检（与 Zed 运行时校验一致）。

自检失败 → 删除该 node 目录重下一次（§2.1 #2：Zed 判定失败也会删目录重下，
版本必须精确——cfg node.version 或默认 v24.11.0，勿改）。

download.py 为 Lane A 并行交付，本模块采用函数内延迟导入（见 toolchain.py 说明）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: 与 crates/node_runtime/src/node_runtime.rs:606 硬编码一致，勿改
DEFAULT_NODE_VERSION = "v24.11.0"

NODE_DIST_BASE = "https://nodejs.org/dist"


@dataclass
class NodePaths:
    """node 运行时路径（linux: <root>/bin/{node,npm}；windows: <root>/{node.exe,npm.cmd}）。"""

    node_bin: Path
    npm_cmd: Path


def _download():
    """延迟导入 download 助手（Lane A 并行交付）。"""
    from . import download  # noqa: PLC0415

    return download


def ensure_node(cfg, platform: str, dist_dir: Path) -> NodePaths:
    """P3 主流程：下载 → 解压 → 自检（幂等：node_bin 存在且自检通过则跳过下载）。"""
    dist_dir = Path(dist_dir)
    is_windows = platform.lower().startswith("windows")
    version = (cfg.get("node") or {}).get("version") or DEFAULT_NODE_VERSION
    if not version.startswith("v"):
        version = "v" + version
    suffix = "win-x64" if is_windows else "linux-x64"
    node_parent = dist_dir / "data" / "node"
    node_root = node_parent / f"node-{version}-{suffix}"
    if is_windows:
        paths = NodePaths(node_bin=node_root / "node.exe", npm_cmd=node_root / "npm.cmd")
    else:
        paths = NodePaths(node_bin=node_root / "bin" / "node", npm_cmd=node_root / "bin" / "npm")

    if _self_check(paths, node_root, is_windows):
        _ensure_runtime_files(node_root)
        print(f"node already present and passed self-check: {paths.node_bin} (skipping download)")
        return paths

    # 下载 → 解压 → 自检；失败则删除目录重下一次（版本必须精确）
    archive = node_parent / (
        f"node-{version}-{suffix}.tar.xz" if not is_windows else f"node-{version}-{suffix}.zip"
    )
    for attempt in (1, 2):
        if node_root.exists():
            shutil.rmtree(node_root)
        node_parent.mkdir(parents=True, exist_ok=True)
        _download_node(version, suffix, archive)
        _extract_node(archive, node_parent)
        _ensure_runtime_files(node_root)
        if _self_check(paths, node_root, is_windows):
            print(f"node self-check passed: {paths.node_bin}")
            return paths
        print(f"WARN: node self-check failed, deleting and re-downloading (attempt {attempt}/2)")
    raise RuntimeError(f"node download/self-check failed ({version}, {suffix}), check network or version")


def _download_node(version: str, suffix: str, archive: Path) -> None:
    """下载 node 官方发行包（linux: tar.xz；windows: zip）。"""
    dl = _download()
    asset = f"node-{version}-{suffix}.tar.xz" if suffix.startswith("linux") else (
        f"node-{version}-{suffix}.zip"
    )
    url = f"{NODE_DIST_BASE}/{version}/{asset}"
    dl.download_file(url, archive)


def _extract_node(archive: Path, node_parent: Path) -> None:
    """解压到 dist/data/node/（根目录为 node-{version}-{suffix}/）。"""
    dl = _download()
    dl.extract_archive(archive, node_parent)


def _self_check(paths: NodePaths, node_root: Path, is_windows: bool) -> bool:
    """结构自检（与 Zed 运行时校验一致）：node 执行 npm-cli.js --version，退出码 0 通过。

    cmd = `{node_bin} {node_root}/node_modules/npm/bin/npm-cli.js --version
           --cache {node_root}/cache --userconfig {node_root}/blank_user_npmrc
           --globalconfig {node_root}/blank_global_npmrc`
    """
    if not paths.node_bin.is_file():
        return False
    # npm 布局：官方 tar 包在 <root>/lib/node_modules/npm（node 24 实测），
    # 源码/旧版可能在 <root>/node_modules/npm —— 两者都试。
    npm_cli = None
    for rel in ("lib/node_modules/npm/bin/npm-cli.js", "node_modules/npm/bin/npm-cli.js"):
        cand = node_root / rel
        if cand.is_file():
            npm_cli = cand
            break
    if npm_cli is None:
        return False
    cmd = [
        str(paths.node_bin),
        str(npm_cli),
        "--version",
        "--cache", str(node_root / "cache"),
        "--userconfig", str(node_root / "blank_user_npmrc"),
        "--globalconfig", str(node_root / "blank_global_npmrc"),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _ensure_runtime_files(node_root: Path) -> None:
    """创建 {node_root}/cache 目录 + touch blank_user_npmrc / blank_global_npmrc（仅当缺失）。"""
    cache_dir = node_root / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for name in ("blank_user_npmrc", "blank_global_npmrc"):
        f = node_root / name
        if not f.exists():
            f.touch()
