"""P4 GitHub 型 LSP：release 查询 → 资产下载 → 解压落位 → metadata + --version 自检。

失败语义（§5 P4）：任一 LSP 失败 → 打印告警 + 加入失败清单，继续下一个
（不 raise、不阻断整体构建）——降级 bundle 仍可用（§2.1 #1：运行时查版本
失败自动回退本地缓存）。

windows 资产名未核验的（package-version-server）以候选名尝试 +
download.github_asset_url 的 release 页 HTML 解析降级；最终失败 → 告警跳过。

download.py 为 Lane A 交付，本模块采用函数内延迟导入（见 toolchain.py 说明）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

#: GitHub 型 LSP 表（§5 P4）：repo / 平台资产模板（{tag} 运行时替换）/ 落位 kind。
#: kind 语义：
#:   gz-single   裸 .gz → 解压为同名（去 .gz）单文件可执行
#:   zip-bin     zip 顶层单目录移平 → <name>_<tag>/bin/<name>(.exe)
#:   nested-bin  <name>-<tag>/<name>-x86_64-unknown-linux-gnu/<name>(.exe)
#:   single-bin  单文件（无 metadata：目录有任意文件即缓存命中）
#: NOTE(未核验): package-version-server 的 windows 资产名待核验——失败降级跳过。
GITHUB_SERVERS: dict[str, dict] = {
    "rust-analyzer": {
        "repo": "rust-lang/rust-analyzer",
        "assets": {
            "linux-x64": "rust-analyzer-x86_64-unknown-linux-gnu.gz",
            "windows-x64": "rust-analyzer-x86_64-pc-windows-msvc.gz",
        },
        "kind": "gz-single",
        "version_check": True,  # 幂等需 --version 成功（§2.1 #1）
    },
    "clangd": {
        "repo": "clangd/clangd",
        "assets": {
            "linux-x64": "clangd-linux-{tag}.zip",
            "windows-x64": "clangd-windows-{tag}.zip",
        },
        "kind": "zip-bin",
        "version_check": True,
    },
    "ty": {
        "repo": "astral-sh/ty",
        "assets": {
            "linux-x64": "ty-x86_64-unknown-linux-gnu.tar.gz",
            "windows-x64": "ty-x86_64-pc-windows-msvc.zip",
        },
        "kind": "nested-bin",
        "version_check": True,
    },
    "ruff": {
        "repo": "astral-sh/ruff",
        "assets": {
            "linux-x64": "ruff-x86_64-unknown-linux-gnu.tar.gz",
            "windows-x64": "ruff-x86_64-pc-windows-msvc.zip",
        },
        "kind": "nested-bin",
        "version_check": True,
    },
    "package-version-server": {
        "repo": "zed-industries/package-version-server",
        "assets": {
            "linux-x64": "package-version-server-x86_64-unknown-linux-gnu.tar.gz",
            "windows-x64": "package-version-server-x86_64-pc-windows-msvc.zip",  # NOTE(未核验)
        },
        "kind": "single-bin",
        # 无 metadata、无 --version 要求：幂等只看文件存在（§5 P4 表）
        "version_check": False,
    },
}


def _download():
    """延迟导入 download 助手（Lane A 并行交付；sys.modules 缓存，重复调用开销可忽略）。"""
    from . import download  # noqa: PLC0415

    return download


def _is_windows(platform: str) -> bool:
    return platform.lower().startswith("windows")


def _exe(platform: str) -> str:
    return ".exe" if _is_windows(platform) else ""


def _archive_dir(dist_dir: Path) -> Path:
    """资产缓存目录（位于 dist 之外，不随 bundle 分发；跨次构建复用实现幂等）。"""
    p = dist_dir.parent / "build" / "lsp-archives"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _tmp_dir(dist_dir: Path, name: str) -> Path:
    """解压临时目录（dist 之外，不随 bundle 分发）。"""
    p = dist_dir.parent / "build" / "lsp-tmp" / name
    shutil.rmtree(p, ignore_errors=True)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _version_ok(bin_path: Path) -> bool:
    """`<bin> --version` 退出码 0（timeout 30s）。"""
    try:
        proc = subprocess.run(
            [str(bin_path), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _write_metadata(path: Path, digest: str) -> None:
    """写 <server>.metadata：{"metadata_version": 1, "digest": <资产字节 sha256>}（§2.1 #1）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"metadata_version": 1, "digest": digest}, indent=2) + "\n")


def _cache_hit(target: Path, metadata: Path | None, digest: str, version_check: bool) -> bool:
    """幂等判定（§2.1 #1）：目标存在 + metadata digest == 当前资产 sha256 + （可选）--version 成功。

    注：digest 由（download_file 缓存的）资产字节算出——缓存命中即零网络；
    single-bin（package-version-server）无 metadata，仅看文件存在。
    """
    if not target.is_file():
        return False
    if metadata is not None:
        if not metadata.is_file():
            return False
        try:
            recorded = json.loads(metadata.read_text()).get("digest")
        except (OSError, ValueError):
            return False
        if recorded != digest:
            return False
    if version_check and not _version_ok(target):
        return False
    return True


def _install_release_server(name: str, plat: str, exe: str, dist_dir: Path) -> None:
    """单个 GitHub release 型 LSP：查最新 tag → 定位资产 → 下载 → 落位 → metadata + 自检。

    失败 raise（由 install_github_lsps 捕获 → 告警 + 失败清单）。
    """
    spec = GITHUB_SERVERS[name]
    dl = _download()
    headers = dl.github_headers()

    tag = dl.get_json(
        f"https://api.github.com/repos/{spec['repo']}/releases/latest",
        headers=headers,
    )["tag_name"]
    asset_name = spec["assets"][plat].replace("{tag}", tag)
    url = dl.github_asset_url(spec["repo"], tag, [spec["assets"][plat]], headers=headers)
    archive = _archive_dir(dist_dir) / asset_name
    dl.download_file(url, archive, headers=headers)  # 幂等：已缓存则零网络
    digest = dl.sha256_of(archive)

    langs = dist_dir / "data" / "languages"
    kind = spec["kind"]
    if kind == "gz-single":
        target = langs / name / f"{name}-{tag}"
        metadata = langs / name / f"{name}-{tag}.metadata"
    elif kind == "single-bin":
        target = langs / name / f"{name}-{tag}"
        metadata = None
    elif kind == "zip-bin":
        target = langs / name / f"{name}_{tag}" / "bin" / f"{name}{exe}"
        metadata = langs / name / f"{name}_{tag}" / "metadata"
    else:  # nested-bin：平台子目录名固定为 linux 资产平台串（windows 同构，仅可执行名带 .exe）
        target = langs / name / f"{name}-{tag}" / f"{name}-x86_64-unknown-linux-gnu" / f"{name}{exe}"
        metadata = langs / name / f"{name}-{tag}.metadata"

    if _cache_hit(target, metadata, digest, spec["version_check"]):
        print(f"  {name} 缓存命中，跳过：{target}")
        return

    tmp = _tmp_dir(dist_dir, name)
    try:
        dl.extract_archive(archive, tmp)
        if kind == "gz-single":
            src = tmp / asset_name[: -len(".gz")]  # 裸 gz → 解压为去 .gz 同名文件
            if not src.is_file():
                raise dl.DownloadError(f"解压后未找到二进制：{src}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            os.chmod(target, 0o755)
        elif kind == "single-bin":
            files = sorted(
                (p for p in tmp.rglob("*") if p.is_file()),
                key=lambda p: len(p.parts),
            )
            if not files:
                raise dl.DownloadError("解压后未找到任何文件")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(files[0], target)
            os.chmod(target, 0o755)
        elif kind == "zip-bin":
            # zip 内顶层单目录 → 整体移平为 <name>_<tag>/（bin/ lib/ 一并落位）
            tops = [p for p in tmp.iterdir() if p.is_dir()]
            if len(tops) != 1:
                raise dl.DownloadError(f"zip 顶层目录数 != 1: {[p.name for p in tops]}")
            dest = target.parent.parent
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(tops[0]), str(dest))
            if not target.is_file():
                raise dl.DownloadError(f"移平后目标缺失：{target}")
            os.chmod(target, 0o755)
        else:  # nested-bin
            matches = sorted(
                (p for p in tmp.rglob(f"{name}{exe}") if p.is_file()),
                key=lambda p: len(p.parts),
            )
            if not matches:
                raise dl.DownloadError(f"解压后未找到 {name}{exe}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(matches[0], target)
            os.chmod(target, 0o755)

        if spec["version_check"] and not _version_ok(target):
            raise RuntimeError(f"{name} --version 自检失败：{target}")
        if metadata is not None:
            _write_metadata(metadata, digest)
        print(f"  {name} 已安装：{target}（tag={tag}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# gopls（可选，cfg lsp.github.gopls=True）：系统 Go 工具链 go install
# ---------------------------------------------------------------------------
def _install_gopls(plat: str, exe: str, dist_dir: Path) -> bool:
    """GOBIN=<languages>/gopls go install golang.org/x/tools/gopls@latest。

    产物复制为 gopls_{ver}_go_{goversion} 前缀文件名；缓存判定：目录内任意
    以 gopls_ 开头的文件即命中。无 go 命令 → 告警跳过（返回 False）。
    """
    langs = dist_dir / "data" / "languages"
    gopls_dir = langs / "gopls"
    if gopls_dir.is_dir() and any(p.name.startswith("gopls_") for p in gopls_dir.iterdir()):
        print("  gopls 缓存命中，跳过")
        return True
    go = shutil.which("go")
    if not go:
        print("  WARN: 未找到 go 命令，gopls 跳过（需系统 Go 工具链）")
        return False
    gopls_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["GOBIN"] = str(gopls_dir)
    try:
        proc = subprocess.run(
            [go, "install", "golang.org/x/tools/gopls@latest"],
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  WARN: go install 执行失败：{exc}")
        return False
    if proc.returncode != 0:
        print(
            f"  WARN: go install 失败（exit={proc.returncode}）："
            f"{(proc.stderr or proc.stdout).strip()[-400:]}"
        )
        return False
    raw = gopls_dir / f"gopls{exe}"
    if not raw.is_file():
        print(f"  WARN: go install 产物缺失：{raw}")
        return False
    ver = _gopls_version(raw)
    go_ver = _go_version(go)
    dest = gopls_dir / f"gopls_{ver}_go_{go_ver}{exe}"
    shutil.copy2(raw, dest)
    os.chmod(dest, 0o755)
    raw.unlink(missing_ok=True)  # 不带 gopls_ 前缀的裸产物不参与缓存判定，删除以免冗余
    print(f"  gopls 已安装：{dest}")
    return True


def _gopls_version(bin_path: Path) -> str:
    """解析 `gopls version` 输出中的 vX.Y.Z（如 "golang.org/x/tools/gopls v0.18.2"）。"""
    try:
        proc = subprocess.run(
            [str(bin_path), "version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        text = proc.stdout or proc.stderr
    except (OSError, subprocess.TimeoutExpired):
        text = ""
    m = re.search(r"v\d+\.\d+\.\d+", text)
    return m.group(0).lstrip("v") if m else "unknown"


def _go_version(go: str) -> str:
    """解析 `go version` 输出中的 goX.Y.Z（如 "go version go1.24.1 linux/amd64"）。"""
    try:
        proc = subprocess.run([go, "version"], capture_output=True, text=True, timeout=30)
        text = proc.stdout or proc.stderr
    except (OSError, subprocess.TimeoutExpired):
        text = ""
    m = re.search(r"go\d+\.\d+(?:\.\d+)?", text)
    return m.group(0) if m else "unknown"


# ---------------------------------------------------------------------------
# 主流程 + finalize 复用的目标路径重算
# ---------------------------------------------------------------------------
def install_github_lsps(cfg, platform: str, dist_dir: Path) -> list[str]:
    """P4 主流程：为每个启用（lsp.github.<name>=True）的 GitHub 型 LSP 下载安装。

    返回失败清单（list[str]）；任一 LSP 失败 → 告警跳过，不 raise、不阻断整体。
    """
    dist_dir = Path(dist_dir)
    plat = "windows-x64" if _is_windows(platform) else "linux-x64"
    exe = _exe(platform)
    enabled = [
        n for n, flag in sorted(((cfg.get("lsp") or {}).get("github") or {}).items()) if flag
    ]
    if not enabled:
        print("无启用的 github 型 LSP，跳过 P4")
        return []

    failed: list[str] = []
    for idx, name in enumerate(enabled, start=1):
        print(f"[P4/{len(enabled)}] github LSP: {name} ...")
        try:
            if name == "gopls":
                if not _install_gopls(plat, exe, dist_dir):
                    failed.append(name)
            elif name in GITHUB_SERVERS:
                _install_release_server(name, plat, exe, dist_dir)
            else:
                print(f"  WARN: 未知 github LSP: {name}（跳过）")
                failed.append(name)
        except Exception as exc:  # noqa: BLE001 —— 单 LSP 失败不阻断整体
            print(f"  WARN: {name} 安装失败：{type(exc).__name__}: {exc}")
            failed.append(name)

    if failed:
        print(f"P4 完成，失败/跳过：{failed}")
    return failed


def lsp_github_target(cfg, platform: str, dist_dir: Path, name: str) -> Path | None:
    """按 P4 落位规则重算目标文件（finalize 产物断言复用；tag 已装 → glob 推断）。

    glob 未命中（从未安装或安装不完整）→ 回退断言其父目录 languages/{name}，
    使 finalize 能区分"装过但路径不定"与"根本没装"（后者目录不存在 → 断言失败）。
    gopls 文件名含动态版本 → 返回 None（finalize 不对其断言）；未知 server → None。
    """
    dist_dir = Path(dist_dir)
    langs = dist_dir / "data" / "languages"
    exe = _exe(platform)
    if name == "gopls" or name not in GITHUB_SERVERS:
        return None
    kind = GITHUB_SERVERS[name]["kind"]
    if kind == "gz-single":
        matches = [
            p for p in langs.glob(f"{name}/{name}-*")
            if p.is_file() and not p.name.endswith(".metadata")
        ]
    elif kind == "single-bin":
        matches = [p for p in langs.glob(f"{name}/{name}-*") if p.is_file()]
    elif kind == "zip-bin":
        matches = list(langs.glob(f"{name}/{name}_*/bin/{name}{exe}"))
    else:  # nested-bin
        matches = list(
            langs.glob(f"{name}/{name}-*/{name}-x86_64-unknown-linux-gnu/{name}{exe}")
        )
    return matches[0] if matches else langs / name
