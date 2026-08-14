"""P5 npm 型 LSP 预安装 + eslint 源码编译（§5 P5 表）。

用 P3 预置的 node（NodePaths）执行安装，保证构建与运行时同一环境。
失败语义：单 server 失败 → 告警跳过 + 加入失败清单，不 raise、不阻断整体。

download.py 为 Lane A 交付，本模块采用函数内延迟导入（见 toolchain.py 说明）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

#: server → (languages 下安装目录名, server_path 相对路径, 依赖/版本约束)
#: server_path 存在即幂等命中（§2.2），无需联网校验。
NPM_SERVERS: dict[str, tuple[str, str, list[str]]] = {
    # 必须 typescript@^6 避开 7.x：Zed 的 typescript adapter 对 7.x 判定
    # 版本不匹配（§2.2）；^6 解析最新 6.x，绝不安装 7.x——勿改。
    "typescript": (
        "typescript-language-server",
        "node_modules/typescript-language-server/lib/cli.mjs",
        ["typescript@^6", "typescript-language-server@latest"],
    ),
    "vtsls": (
        "vtsls",
        "node_modules/@vtsls/language-server/bin/vtsls.js",
        ["@vtsls/language-server@latest"],
    ),
    "yaml": (
        "yaml-language-server",
        "node_modules/yaml-language-server/bin/yaml-language-server",
        ["yaml-language-server@latest"],
    ),
    "css": (
        "vscode-css-language-server",
        "node_modules/vscode-langservers-extracted/bin/vscode-css-language-server",
        ["vscode-langservers-extracted@latest"],
    ),
    "bash": (
        "bash-language-server",
        "node_modules/bash-language-server/out/cli.js",
        ["bash-language-server@latest"],
    ),
    "tailwind": (
        "tailwindcss-language-server",
        "node_modules/.bin/tailwindcss-language-server",
        ["tailwindcss-language-server@latest"],
    ),
    "pyright": (
        "pyright",
        "node_modules/pyright/langserver.index.js",
        ["pyright@latest"],
    ),
    "basedpyright": (
        "basedpyright",
        "node_modules/basedpyright/langserver.index.js",
        ["basedpyright@latest"],
    ),
}

#: eslint 固定版本（§5 P5 / §2.1 #8）：源码打包而非 npm 二进制，固定 3.0.24
ESLINT_VERSION = "3.0.24"
ESLINT_ARCHIVE_URL = (
    f"https://github.com/microsoft/vscode-eslint/archive/refs/tags/"
    f"release%2F{ESLINT_VERSION}.tar.gz"
)


def _download():
    """延迟导入 download 助手（Lane A 并行交付；sys.modules 缓存，重复调用开销可忽略）。"""
    from . import download  # noqa: PLC0415

    return download


def _q(arg: str) -> str:
    """windows cmd 引号包裹（npm 参数含 @ ^ 等 cmd 特殊字符；^ 为 cmd 转义符必须引住）。"""
    return '"' + arg.replace('"', '\\"') + '"'


def _run_npm(np, args: list[str], *, timeout: int, cwd: Path | None = None, is_windows: bool = False):
    """执行 npm 子进程，返回 CompletedProcess；启动失败/超时返回 None。

    注释：windows 下 npm.cmd 是批处理脚本，无法直接 execve，须经 cmd.exe
    解析（shell=True）；参数逐个双引号包裹防 cmd 特殊字符。
    """
    try:
        if is_windows:
            cmdline = f'"{np.npm_cmd}" ' + " ".join(_q(a) for a in args)
            return subprocess.run(
                cmdline,
                shell=True,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        return subprocess.run(
            [str(np.npm_cmd), *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _install_eslint(np, dist_dir: Path, is_windows: bool) -> None:
    """eslint 3.0.24：下载源码归档 → 唯一目录 rename 为 vscode-eslint → npm install+compile。

    幂等：server_path 存在即跳过（§2.1 #8，构建与运行时同机校验通过后零网络）。
    """
    dl = _download()
    langs = dist_dir / "data" / "languages"
    versioned = langs / "eslint" / f"vscode-eslint-{ESLINT_VERSION}"
    repo_root = versioned / "vscode-eslint"
    server_path = repo_root / "server" / "out" / "eslintServer.js"
    if server_path.is_file():
        print(f"  eslint 缓存命中，跳过：{server_path}")
        return

    archive = dist_dir.parent / "build" / "lsp-archives" / f"vscode-eslint-{ESLINT_VERSION}.tar.gz"
    dl.download_file(ESLINT_ARCHIVE_URL, archive)
    tmp = dist_dir.parent / "build" / "lsp-tmp" / "eslint"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    dl.extract_archive(archive, tmp)
    tops = [p for p in tmp.iterdir() if p.is_dir()]
    if len(tops) != 1:
        raise RuntimeError(f"eslint 归档顶层目录数 != 1: {[p.name for p in tops]}")
    # 唯一解压目录 rename 为 vscode-eslint（§5 P5）
    if repo_root.exists():
        shutil.rmtree(repo_root)
    versioned.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tops[0]), str(repo_root))

    for step_args, step_label in ((["install"], "npm install"), (["run", "compile"], "npm run compile")):
        proc = _run_npm(np, step_args, timeout=900, cwd=repo_root, is_windows=is_windows)
        if proc is None or proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[-400:] if proc else "subprocess 启动失败"
            raise RuntimeError(f"eslint {step_label} 失败（exit={getattr(proc, 'returncode', '?')}）：{detail}")
    if not server_path.is_file():
        raise RuntimeError(f"eslint 编译后 server_path 缺失：{server_path}")
    print(f"  eslint 已安装：{server_path}")


def install_npm_lsps(cfg, np, platform: str, dist_dir: Path) -> list[str]:
    """P5 主流程：为每个启用（lsp.npm.<name>=True）的 npm 型 LSP 预安装。

    返回失败清单（list[str]）；任一 server 失败 → 告警跳过，不 raise、不阻断整体。
    """
    dist_dir = Path(dist_dir)
    is_windows = platform.lower().startswith("windows")
    enabled = [
        n for n, flag in sorted(((cfg.get("lsp") or {}).get("npm") or {}).items()) if flag
    ]
    if not enabled:
        print("无启用的 npm 型 LSP，跳过 P5")
        return []

    failed: list[str] = []
    for idx, name in enumerate(enabled, start=1):
        print(f"[P5/{len(enabled)}] npm LSP: {name} ...")
        try:
            if name == "eslint":
                _install_eslint(np, dist_dir, is_windows)
                continue
            if name not in NPM_SERVERS:
                print(f"  WARN: 未知 npm LSP: {name}（跳过）")
                failed.append(name)
                continue
            install_dir_name, rel_path, pkgs = NPM_SERVERS[name]
            install_dir = dist_dir / "data" / "languages" / install_dir_name
            server_path = install_dir / rel_path
            if server_path.is_file():
                print(f"  {name} 缓存命中，跳过：{server_path}")
                continue
            proc = _run_npm(
                np,
                ["--prefix", str(install_dir), "install", *pkgs, "--save-exact"],
                timeout=600,
                is_windows=is_windows,
            )
            if proc is None or proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).strip()[-400:] if proc else "subprocess 启动失败"
                print(
                    f"  WARN: {name} npm install 失败"
                    f"（exit={getattr(proc, 'returncode', '?')}）：{detail}"
                )
                failed.append(name)
                continue
            if not server_path.is_file():
                print(f"  WARN: {name} 安装后 server_path 缺失：{server_path}")
                failed.append(name)
                continue
            print(f"  {name} 已安装：{server_path}")
        except Exception as exc:  # noqa: BLE001 —— 单 server 失败不阻断整体
            print(f"  WARN: {name} 安装失败：{type(exc).__name__}: {exc}")
            failed.append(name)

    if failed:
        print(f"P5 完成，失败/跳过：{failed}")
    return failed


def npm_server_path(cfg, platform: str, dist_dir: Path, name: str) -> Path | None:
    """按 P5 规则重算 server_path（finalize 产物断言复用）；未知 server 返回 None。"""
    del platform  # server_path 双平台一致（.bin 无扩展名 shim 两平台均存在）
    dist_dir = Path(dist_dir)
    langs = dist_dir / "data" / "languages"
    if name == "eslint":
        return (
            langs / "eslint" / f"vscode-eslint-{ESLINT_VERSION}" / "vscode-eslint"
            / "server" / "out" / "eslintServer.js"
        )
    if name not in NPM_SERVERS:
        return None
    install_dir_name, rel_path, _ = NPM_SERVERS[name]
    return langs / install_dir_name / rel_path
