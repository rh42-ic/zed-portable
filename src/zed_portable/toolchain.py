"""P1 zed 二进制：官方 release 下载 / 本地路径复制 + 版本解析。

本模块只保证 zed 本体（P1 职责）；WASI SDK 与 zed-extension CLI 不再随 P1
强制获取——由 P2 扩展的兜底路径（本地打包）按需惰性获取，见
``ensure_wasi_sdk`` / ``ensure_zed_extension_cli``（P2 主路径官方 API 直下
打包产物时无需二者，Toolchain 对应字段保持 None）。

本机禁 rust 编译（AGENTS.md 硬约束）——zed-extension CLI 一律走预编译下载分支；
`cargo build -p extension_cli` 仅作为 CI 兜底说明（见 `_ensure_zed_extension_cli` 的
raise 提示），本模块不触发任何编译。

download.py 为 Lane A 并行交付，本模块采用函数内延迟导入（sys.modules 缓存使重复
导入开销可忽略），保证模块在 download.py 落位前即可导入、语法/导入级自检可独立执行。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

#: WASI SDK 固定版本（github.com/WebAssembly/wasi-sdk releases tag，与 zed 上游 CI 对齐）
WASI_SDK_TAG = "wasi-sdk-25"

#: WASI SDK 资产名（按平台）
WASI_SDK_ASSETS = {
    "linux-x64": "wasi-sdk-25.0-x86_64-linux.tar.gz",
    "windows-x64": "wasi-sdk-25.0-x86_64-windows.tar.gz",
}

#: zed-extension CLI 预编译产物发布空间（官方 CI 上传到 nyc3 digitaloceanspaces）
ZED_EXTENSION_CLI_BASE = "https://zed-extension-cli.nyc3.digitaloceanspaces.com"

#: zed-extension CLI 平台路径
#: NOTE(未核验): windows 路径 `x86_64-pc-windows-msvc/zed-extension.exe` 待核验——
#: CI windows job 首跑时按实际发布物修正。
ZED_EXTENSION_CLI_PLATFORM = {
    "linux-x64": "x86_64-unknown-linux-gnu/zed-extension",
    "windows-x64": "x86_64-pc-windows-msvc/zed-extension.exe",
}

#: zed 官方 release 资产名候选（linux 为 tar 包，release.yml:453-454；windows 为
#: Inno Setup 安装器 Zed-x86_64.exe——GUI 向导，非便携可执行文件，下载后须静默
#: 安装提取，见 _run_zed_installer / _collect_windows_install；release.yml:723-724）
ZED_ASSET_CANDIDATES = {
    "linux-x64": ["zed-linux-x86_64.tar.gz"],
    "windows-x64": ["Zed-x86_64.exe"],
}


@dataclass
class Toolchain:
    """P1 产物：zed 二进制 + 版本信息。

    wasi_sdk_path / zed_extension 默认 None（惰性）——仅 P2 扩展兜底路径
    （本地打包）按需通过 ``toolchain.ensure_wasi_sdk`` /
    ``toolchain.ensure_zed_extension_cli`` 获取并回填。
    """

    zed_bin: Path
    zed_tag: str
    zed_commit: str
    wasi_sdk_path: Path | None = None
    zed_extension: Path | None = None


def _download():
    """延迟导入 download 助手（Lane A 并行交付；sys.modules 缓存，重复调用开销可忽略）。"""
    from . import download  # noqa: PLC0415

    return download


def _is_windows(platform: str) -> bool:
    return platform.lower().startswith("windows")


def resolve_zed_tag(cfg, token: Optional[str] = None) -> Tuple[str, str]:
    """解析 Zed release tag 及对应 commit sha，返回 ``(tag, commit_sha)``。

    - ``cfg.zed.release_tag`` 非空 → 直接使用该 tag，仅解析 commit（tag 对象循环
      跟随 ``git/tags/{sha}`` 直到指向 commit）。
    - 空 → 按 channel：``stable`` 取 ``/releases/latest`` 的 tag_name；
      ``preview``/``dev`` 取 ``/releases?per_page=1`` 第一个（含 prerelease）tag_name，
      再解析 commit。
    """
    dl = _download()
    headers = dl.github_headers()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    zed_cfg = cfg.get("zed") or {}
    release_tag = (zed_cfg.get("release_tag") or "").strip()
    if release_tag:
        return release_tag, _resolve_tag_commit(release_tag, headers)
    channel = (zed_cfg.get("channel") or "stable").strip().lower()
    if channel == "stable":
        data = dl.get_json(
            "https://api.github.com/repos/zed-industries/zed/releases/latest",
            headers=headers,
        )
        tag = data["tag_name"]
    else:  # preview / dev → 最新 release（含 prerelease）
        data = dl.get_json(
            "https://api.github.com/repos/zed-industries/zed/releases?per_page=1",
            headers=headers,
        )
        tag = data[0]["tag_name"]
    return tag, _resolve_tag_commit(tag, headers)


def _resolve_tag_commit(tag: str, headers: dict) -> str:
    """``git/ref/tags/{tag}`` → 若 object.type == "tag" 再取 tag 对象指向，直到 commit。"""
    dl = _download()
    ref = dl.get_json(
        f"https://api.github.com/repos/zed-industries/zed/git/ref/tags/{tag}",
        headers=headers,
    )
    sha = ref["object"]["sha"]
    obj_type = ref["object"]["type"]
    for _ in range(5):  # 防御性循环上限（正常 1-2 跳）
        if obj_type == "commit":
            return sha
        if obj_type != "tag":
            break
        tag_obj = dl.get_json(
            f"https://api.github.com/repos/zed-industries/zed/git/tags/{sha}",
            headers=headers,
        )
        sha = tag_obj["object"]["sha"]
        obj_type = tag_obj["object"]["type"]
    raise RuntimeError(f"cannot resolve tag {tag!r} to a commit (final object.type={obj_type!r})")


def ensure_zed_binary(cfg, platform: str, build_dir: Path, dist_dir: Path) -> Toolchain:
    """P1 主流程：仅 zed 二进制（download/本地路径）+ zed_tag/zed_commit 解析（幂等）。

    不再强制下载 WASI SDK / zed-extension CLI——返回的 Toolchain 对应字段为
    None，由 P2 扩展兜底路径惰性获取（见 ensure_wasi_sdk / ensure_zed_extension_cli）。
    """
    dl = _download()
    build_dir = Path(build_dir)
    dist_dir = Path(dist_dir)
    is_windows = _is_windows(platform)
    plat = "windows-x64" if is_windows else "linux-x64"
    exe = ".exe" if is_windows else ""

    # zed 二进制（binary=download → 官方 release；路径 → 本地复制）
    binary = (cfg.get("zed") or {}).get("binary") or "download"
    zed_bin = dist_dir / "bin" / f"zed{exe}"
    zed_tag: str = ""
    zed_commit: str = ""
    if binary == "download":
        if _is_valid_zed(zed_bin):
            print(f"zed binary already present and verified: {zed_bin} (skipping download)")
            # 幂等跳过不重新解析 tag——但若 cfg release_tag 空，仍查一次用于返回
            release_tag = (cfg.get("zed") or {}).get("release_tag") or ""
            if not release_tag and not zed_tag:
                zed_tag, zed_commit = resolve_zed_tag(cfg)
            elif release_tag and not zed_tag:
                zed_tag, zed_commit = release_tag, ""
        else:
            zed_tag, zed_commit = resolve_zed_tag(cfg)
            _download_zed(zed_tag, plat, build_dir, dist_dir)
    else:
        _copy_local_zed(binary, zed_bin, is_windows)
        zed_tag, zed_commit = "local", ""

    return Toolchain(
        zed_bin=zed_bin,
        zed_tag=zed_tag,
        zed_commit=zed_commit,
        wasi_sdk_path=None,
        zed_extension=None,
    )


# ---------------------------------------------------------------------------
# WASI SDK（惰性：P2 扩展兜底路径按需获取）
# ---------------------------------------------------------------------------
def ensure_wasi_sdk(build_dir: Path, platform: str) -> Path:
    """惰性获取 WASI SDK v25（P2 兜底路径用）；幂等：build/wasi-sdk 存在即跳过。

    返回内部 wasi-sdk-25.0-* 目录；下载/解压失败 raise（调用方告警跳过该扩展）。
    """
    return _ensure_wasi_sdk(Path(build_dir), _is_windows(platform))


def _ensure_wasi_sdk(build_dir: Path, is_windows: bool) -> Path:
    """下载并解压 WASI SDK v25，返回内部 wasi-sdk-25.0-* 目录。幂等：build/wasi-sdk 存在即跳过。"""
    dl = _download()
    sdk_root = build_dir / "wasi-sdk"
    if sdk_root.is_dir():
        inner = _find_wasi_sdk_dir(sdk_root)
        print(f"WASI SDK already present: {inner} (skipping download)")
        return inner
    plat = "windows-x64" if is_windows else "linux-x64"
    asset = WASI_SDK_ASSETS[plat]
    url = (
        f"https://github.com/WebAssembly/wasi-sdk/releases/download/"
        f"{WASI_SDK_TAG}/{asset}"
    )
    archive = build_dir / "wasi-sdk.tar.gz"
    dl.download_file(url, archive)
    sdk_root.mkdir(parents=True, exist_ok=True)
    dl.extract_archive(archive, sdk_root)
    return _find_wasi_sdk_dir(sdk_root)


def _find_wasi_sdk_dir(sdk_root: Path) -> Path:
    """glob 找含 wasi-sdk-25 的目录（内部目录名形如 wasi-sdk-25.0-x86_64-linux）。"""
    matches = [p for p in sdk_root.iterdir() if p.is_dir() and "wasi-sdk-25" in p.name]
    if not matches:
        raise RuntimeError(f"wasi-sdk-25* directory not found under {sdk_root} (extraction failed?)")
    matches.sort(key=lambda p: len(p.name))
    return matches[0]


# ---------------------------------------------------------------------------
# zed-extension CLI（惰性：P2 扩展兜底路径按需获取）
# ---------------------------------------------------------------------------
def ensure_zed_extension_cli(build_dir: Path, platform: str, zed_commit: str) -> Path:
    """惰性获取 zed-extension CLI（P2 兜底路径用）。

    幂等：build/zed-extension(.exe) 已存在且可执行 → 直接返回；否则下载
    （预编译，本机禁 rust 编译），失败 raise（提示 CI cargo build 兜底）。
    """
    is_windows = _is_windows(platform)
    cli_path = Path(build_dir) / f"zed-extension{'.exe' if is_windows else ''}"
    if cli_path.is_file() and os.access(cli_path, os.X_OK):
        print(f"zed-extension CLI already present: {cli_path} (skipping download)")
        return cli_path
    _ensure_zed_extension_cli(cli_path, zed_commit, is_windows)
    return cli_path


def _ensure_zed_extension_cli(cli_path: Path, zed_commit: str, is_windows: bool) -> None:
    """下载预编译 zed-extension CLI（本机禁 rust 编译，不走 cargo build）。

    CLI 按 commit 发布到 DO bucket，官方用 zed 仓库移动指针 tags/extension-cli
    标记当前可用版本（extensions 仓库 ci.yml 亦固定同一 sha）；release tag 的
    commit 通常没有对应 CLI 产物（403）——先解析 extension-cli 标签，失败才回退
    release commit。
    """
    dl = _download()
    plat = "windows-x64" if is_windows else "linux-x64"
    cli_sha = _extension_cli_sha(dl.github_headers()) or zed_commit
    url = f"{ZED_EXTENSION_CLI_BASE}/{cli_sha}/{ZED_EXTENSION_CLI_PLATFORM[plat]}"
    try:
        dl.download_file(url, cli_path)
    except dl.DownloadError as exc:
        raise RuntimeError(
            f"zed-extension CLI download failed: {url}\n{exc}\n"
            "CI can fall back to cargo build -p extension_cli (rust compilation forbidden on this machine)"
        ) from exc
    if not is_windows:
        os.chmod(cli_path, 0o755)
    print(f"zed-extension CLI ready: {cli_path} (cli_sha={cli_sha})")


def _extension_cli_sha(headers: dict) -> str:
    """解析 zed 仓库 tags/extension-cli（publish_extension_cli 工作流的移动指针）。

    返回 commit sha；解析失败返回空串（调用方回退 release commit）。
    """
    dl = _download()
    try:
        ref = dl.get_json(
            "https://api.github.com/repos/zed-industries/zed/git/ref/tags/extension-cli",
            headers=headers,
        )
        obj = ref.get("object") or {}
        return obj.get("sha", "") if obj.get("type") == "commit" else ""
    except Exception:  # noqa: BLE001 —— 网络/解析失败一律回退
        return ""


# ---------------------------------------------------------------------------
# zed 二进制
# ---------------------------------------------------------------------------
def _download_zed(tag: str, plat: str, build_dir: Path, dist_dir: Path) -> None:
    """按平台获取 zed 官方 release 资产并落位 dist/bin/zed(.exe)，随后校验。

    linux tar（release.yml:453-454）解压后是 zed.app/ 应用包：bin/zed 是启动器，
    真实编辑器在 libexec/zed-editor，lib/ 内置运行时 .so，share/ 为桌面集成。
    复制必须保持相对布局（启动器按 `../libexec/zed-editor` 定位 bundle）：
    bin/ → dist/bin、libexec/ → dist/libexec、lib/ → dist/lib、share/ → dist/share。

    windows 官方无便携 zip，唯一发布资产是 Inno Setup 安装器（Zed-x86_64.exe，
    GUI 向导，release.yml:723-724）——直接下载为 exe 运行 `--version` 会挂起超时。
    须静默安装（/VERYSILENT /DIR=）到 build 目录后按 zed.iss [Files] 布局提取：
    Zed.exe（GUI，windows_subsystem）→ dist 根、bin/zed.exe（cli，console 程序，
    校验用它）→ dist/bin。cli 通过 `../Zed.exe` 相对定位 GUI 编辑器
    （crates/cli/src/main.rs possible_locations），故 dist 内相对布局必须保持。
    """
    dl = _download()
    is_windows = plat.startswith("windows")
    candidates = ZED_ASSET_CANDIDATES[plat]
    url = dl.github_asset_url("zed-industries/zed", tag, candidates)
    bin_dir = dist_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if is_windows:
        installer = build_dir / "Zed-x86_64.exe"
        dl.download_file(url, installer)
        install_dir = build_dir / "zed-windows-install"
        _run_zed_installer(installer, install_dir)
        _collect_windows_install(install_dir, dist_dir)
    else:
        archive = build_dir / "zed-linux-x86_64.tar.gz"
        dl.download_file(url, archive)
        extract_dir = build_dir / "zed-linux-x86_64"
        if extract_dir.is_dir():
            shutil.rmtree(extract_dir)
        dl.extract_archive(archive, extract_dir)
        app = _find_zed_app(extract_dir)
        shutil.copy2(app / "bin" / "zed", bin_dir / "zed")
        os.chmod(bin_dir / "zed", 0o755)
        for sub in ("libexec", "lib", "share"):
            src = app / sub
            if src.is_dir():
                shutil.copytree(src, dist_dir / sub, dirs_exist_ok=True)
    # 下载后校验：存在 + 可执行 + --version 退出码 0（失败 raise）
    _verify_zed(bin_dir / ("zed.exe" if is_windows else "zed"))


def _run_zed_installer(installer: Path, install_dir: Path) -> None:
    """静默运行 Inno Setup 安装器（Zed-x86_64.exe）到 install_dir（用户级，无 UAC）。

    参数与 scoop 社区验证一致：/VERYSILENT（无 UI 向导）、/SUPPRESSMSGBOXES、
    /NORESTART、/DIR= 覆盖默认安装目录（{autopf}\\Zed）、空 /TASKS= 禁用
    addtopath/associatewithfiles 等注册表任务（[Run] 启动 GUI 段有 WizardNotSilent
    守卫，静默模式不执行）。列表直传 subprocess（无 shell），`/TASKS=` 作为单个
    字符串元素，不受 shell 空串语义影响。
    """
    install_dir = Path(install_dir)
    args = [
        str(installer),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        f"/DIR={install_dir}",
        "/TASKS=",
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Zed installer timed out (600s): {installer}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"Zed installer exit code {proc.returncode} (stdout={proc.stdout.strip()!r} "
            f"stderr={proc.stderr.strip()!r}): {installer}"
        )


def _collect_windows_install(install_dir: Path, dist_dir: Path) -> None:
    """把静默安装产物按 bundle 布局复制到 dist。

    布局约束（crates/zed/resources/windows/zed.iss [Files] 段）：
    - Zed.exe → dist/Zed.exe（GUI 编辑器，必需，缺失 raise）
    - bin/zed.exe（cli 改名）→ dist/bin/zed.exe（缺失时交给 _verify_zed 报错）
    - conpty.dll → dist/；amd_ags_x64.dll → dist/（aarch64 安装器无此文件，容错）
    - x64/OpenConsole.exe → dist/x64/；arm64/OpenConsole.exe → dist/arm64/

    cli 通过 `../Zed.exe` 定位 GUI 编辑器（crates/cli/src/main.rs possible_locations），
    故 dist/bin/zed.exe 必须与 dist/Zed.exe 保持相对布局。
    """
    install_dir = Path(install_dir)
    dist_dir = Path(dist_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)
    gui = install_dir / "Zed.exe"
    if not gui.is_file():
        raise RuntimeError(f"install directory missing GUI editor Zed.exe: {install_dir}")
    shutil.copy2(gui, dist_dir / "Zed.exe")
    _copy_windows_file(install_dir / "bin" / "zed.exe", dist_dir / "bin" / "zed.exe")
    _copy_windows_file(install_dir / "conpty.dll", dist_dir / "conpty.dll")
    _copy_windows_file(install_dir / "amd_ags_x64.dll", dist_dir / "amd_ags_x64.dll")
    _copy_windows_file(
        install_dir / "x64" / "OpenConsole.exe", dist_dir / "x64" / "OpenConsole.exe"
    )
    _copy_windows_file(
        install_dir / "arm64" / "OpenConsole.exe", dist_dir / "arm64" / "OpenConsole.exe"
    )


def _copy_windows_file(src: Path, dst: Path) -> None:
    """源存在时复制（aarch64/x64 差异文件容错）；目标父目录自动创建。"""
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _find_zed_app(extract_dir: Path) -> Path:
    """在解压目录中找 zed.app 应用包目录（linux tar 顶层）。"""
    matches = [p for p in extract_dir.iterdir() if p.is_dir() and p.name.endswith(".app")]
    if not matches:
        # 兜底：布局变化时退化为最浅名为 zed 的可执行文件所在的父目录
        zed = _find_zed_binary(extract_dir)
        return zed.parent
    matches.sort(key=lambda p: len(p.name))
    return matches[0]


def _find_zed_binary(extract_dir: Path) -> Path:
    """在解压目录中找名为 zed 的普通文件（取层级最浅者）。"""
    for p in sorted(extract_dir.rglob("zed"), key=lambda p: len(p.parts)):
        if p.is_file():
            return p
    raise RuntimeError(f"no executable named zed found in extraction dir {extract_dir}")


def _copy_local_zed(binary: str, zed_bin: Path, is_windows: bool) -> None:
    """cfg zed.binary 为本地路径：直接复制到 dist/bin/zed(.exe) 并校验。"""
    src = Path(binary)
    if not src.is_file():
        raise RuntimeError(f"local zed binary does not exist: {src} (cfg zed.binary={binary!r})")
    zed_bin.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, zed_bin)
    os.chmod(zed_bin, 0o755)
    _verify_zed(zed_bin)


def _is_valid_zed(bin_path: Path) -> bool:
    """幂等判定：存在 + 可执行 + --version 退出码 0。"""
    try:
        _verify_zed(bin_path)
        return True
    except (OSError, RuntimeError):
        return False


def _verify_zed(bin_path: Path) -> None:
    """校验 zed 二进制：存在 + 可执行 + `--version` 退出码 0（timeout 30s）；失败 raise。"""
    if not bin_path.is_file():
        raise RuntimeError(f"zed binary does not exist: {bin_path}")
    if not os.access(bin_path, os.X_OK):
        raise RuntimeError(f"zed binary is not executable: {bin_path}")
    try:
        proc = subprocess.run(
            [str(bin_path), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"zed --version timed out (30s): {bin_path}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"zed --version exit code {proc.returncode} (stdout={proc.stdout.strip()!r} "
            f"stderr={proc.stderr.strip()!r})"
        )
    version_line = proc.stdout.strip() or proc.stderr.strip()
    print(f"zed verification passed: {version_line}")
