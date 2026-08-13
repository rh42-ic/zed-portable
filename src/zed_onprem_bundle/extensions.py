"""P2 扩展打包：submodule 拉取 → zed-extension 打包 → dist/data/extensions/installed/ 落位。

依赖说明（§5.5）：
- 含 LSP 的扩展（如 gleam/deno/latex）离线时仅语法高亮——其 LSP 由扩展运行时
  自行下载到 work/{id}/，无通用预置路径，本阶段不做 LSP 线索探测、不阻断构建；
  LSP 待办清单由 §5.5 分类在文档中打印，需要完整 LSP 的扩展由后续阶段/人工按其
  机制预置。
- 平台无关：wasm 打包 linux/windows 双平台共用同一 zed-extension 产物
  （windows 下 zed-extension.exe 同样调用）。

download.py 为 Lane A 并行交付，本模块采用函数内延迟导入（见 toolchain.py 说明）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List


def _download():
    """延迟导入 download 助手（Lane A 并行交付）。"""
    from . import download  # noqa: PLC0415

    return download


def build_extensions(
    cfg,
    tc,
    platform: str,
    build_dir: Path,
    dist_dir: Path,
    extensions_repo: str,
) -> List[str]:
    """为每个配置的扩展执行 submodule init → 打包 → 落位，返回跳过/失败的 id 清单。

    不 raise：每个扩展独立容错（submodule 失败 / 打包失败 / 产物缺失 → 告警跳过）。
    """
    ids = list((cfg.get("extensions") or {}).get("ids") or [])
    if not ids:
        print("无扩展配置，跳过 P2")
        return []
    repo = Path(extensions_repo)
    if not repo.is_dir():
        print(f"WARN: extensions 仓库不存在：{repo}（全部跳过）")
        return ids
    build_dir = Path(build_dir)
    installed_root = Path(dist_dir) / "data" / "extensions" / "installed"
    scratch = build_dir / "ext" / "scratch"
    skipped: List[str] = []
    for idx, eid in enumerate(ids, start=1):
        print(f"[{idx}/{len(ids)}] {eid} ...")
        installed_dir = installed_root / eid
        if _already_installed(installed_dir):
            print(f"  已存在，跳过：{installed_dir}")
            continue
        if not _init_submodule(eid, repo):
            skipped.append(eid)
            continue
        out_dir = build_dir / "ext" / "out" / eid
        if not _package_extension(tc, eid, repo, scratch, out_dir):
            skipped.append(eid)
            continue
        if not _place_extension(eid, out_dir, installed_dir):
            skipped.append(eid)
    return skipped


def _init_submodule(eid: str, repo: Path) -> bool:
    """git submodule update --init --depth 1 extensions/{id}（cwd=extensions 仓库）。"""
    try:
        proc = subprocess.run(
            ["git", "submodule", "update", "--init", "--depth", "1", f"extensions/{eid}"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        print(f"  WARN: submodule init 超时（300s）：{eid}")
        return False
    except OSError as exc:
        print(f"  WARN: 无法执行 git submodule：{exc}")
        return False
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        print(f"  WARN: submodule init 失败：{eid}（{detail[-400:]}）")
        return False
    return True


def _package_extension(tc, eid: str, repo: Path, scratch: Path, out_dir: Path) -> bool:
    """zed-extension --scratch-dir ... --source-dir ... --output-dir ...（WASI_SDK_PATH 注入）。"""
    scratch.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["WASI_SDK_PATH"] = str(tc.wasi_sdk_path)
    cmd = [
        str(tc.zed_extension),
        "--scratch-dir", str(scratch),
        "--source-dir", str(repo / "extensions" / eid),
        "--output-dir", str(out_dir),
    ]
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        print(f"  WARN: 打包超时（600s）：{eid}")
        return False
    except OSError as exc:
        print(f"  WARN: zed-extension 无法执行（{tc.zed_extension}）：{exc}")
        return False
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        print(f"  WARN: 打包失败：{eid}（exit={proc.returncode}）：{detail[-400:]}")
        return False
    return True


def _place_extension(eid: str, out_dir: Path, installed_dir: Path) -> bool:
    """输出目录内容复制到 dist/data/extensions/installed/{id}/，删 manifest.json，校验产物。"""
    if not out_dir.is_dir():
        print(f"  WARN: 打包输出目录缺失：{out_dir}")
        return False
    if installed_dir.exists():
        shutil.rmtree(installed_dir)
    shutil.copytree(out_dir, installed_dir)
    # manifest.json 为发布元数据（上传用），运行时不需要，删除
    manifest = installed_dir / "manifest.json"
    if manifest.is_file():
        manifest.unlink()
    if not _already_installed(installed_dir):
        # 缺 extension.wasm / extension.toml → 告警跳过，并清理半成品避免运行时加载坏扩展
        shutil.rmtree(installed_dir, ignore_errors=True)
        print(f"  WARN: 落位后缺少 extension.wasm / extension.toml：{eid}")
        return False
    print(f"  已落位：{installed_dir}")
    return True


def _already_installed(installed_dir: Path) -> bool:
    """幂等判定：extension.wasm 或 extension.toml 存在即视为已安装。"""
    return (installed_dir / "extension.wasm").is_file() or (
        installed_dir / "extension.toml"
    ).is_file()
