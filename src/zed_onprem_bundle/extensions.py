"""P2 扩展：主路径官方 API 直下打包产物；submodule+zed-extension 本地打包为兜底。

主路径（默认，每扩展独立容错）：
    1. GET https://api.zed.dev/extensions/{id} → 元数据多版本历史数组
       （已实测：200 返回 {"data": [{id,name,version,schema_version,
       wasm_api_version,provides,published_at,download_count,...}]}）。
    2. 兼容过滤：schema_version（缺省 0）<= MAX_SCHEMA_VERSION 且
       wasm_api_version 若存在则 <= MAX_WASM_API_VERSION（缺失视为兼容）→
       取 semver 最大版本。无兼容版本 → 告警跳过（不走兜底——服务端无兼容
       产物时源码打包也未必兼容）。
    3. 幂等版本比对：installed/{id}/extension.toml 顶层 version 与最新兼容版
       相等 → 跳过；toml 解析失败/无 version → 保守跳过（视为已装，避免误重装）。
    4. GET https://api.zed.dev/extensions/{id}/{version}/download → 302
       重定向到 S3 presigned URL（3 分钟有效）→ tar.gz（requests.get 自动
       跟随 302；**不要用 HEAD 预检**——axum get 路由不匹配 HEAD，会 404）。
       下载到 build/ext/archives/{id}-{version}.tar.gz（download_file 幂等）。
    5. 解压到 build/ext/install-tmp/{id}/ → 校验 extension.toml + extension.wasm
       （官方打包产物无 manifest.json）→ 原子替换 installed/{id}。校验失败 →
       清理 installed 防坏扩展 + 告警跳过（不走兜底——api 产物损坏说明上游异常）。

兜底（仅元数据 GET 网络失败或产物下载失败 DownloadError）：
    extensions 仓库 submodule 拉取 → zed-extension 本地打包 → 落位（原 P2 流程）。
    工具链惰性：WASI SDK / zed-extension CLI 仅在本路径用到，通过
    ``toolchain.ensure_wasi_sdk`` / ``toolchain.ensure_zed_extension_cli``
    按需获取并回填 tc（获取失败 → 告警跳过该扩展，不 raise）。

兼容常量（来自 zed 源码，勿改）：
    - crates/extension_host/src/extension_host.rs:75  CURRENT_SCHEMA_VERSION = SchemaVersion(1)
    - crates/extension_host/src/wasm_host/wit.rs:60-69  stable/preview 的
      wasm_api_version_range = 0.0.1..=0.7.0

依赖说明（§5.5）：含 LSP 的扩展（如 gleam/deno/latex）离线时仅语法高亮——其
LSP 由扩展运行时自行下载到 work/{id}/，无通用预置路径，本阶段不做 LSP 线索
探测、不阻断构建。

download.py / toolchain.py 采用函数内延迟导入（与 toolchain.py 同风格，
sys.modules 缓存使重复导入开销可忽略）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover —— pyproject 要求 >=3.11，此处仅防御
    import tomli as tomllib  # type: ignore[no-redef]

#: Zed 官方扩展注册中心 API 根
ZED_EXTENSIONS_API = "https://api.zed.dev/extensions"
#: 最大兼容 schema_version（crates/extension_host/src/extension_host.rs:75）
MAX_SCHEMA_VERSION = 1
#: 最大兼容 wasm_api_version（crates/extension_host/src/wasm_host/wit.rs:60-69，stable/preview）
MAX_WASM_API_VERSION = "0.7.0"


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
    """为每个配置的扩展执行 官方 API 直下（主）/ 本地打包（兜底），返回跳过/失败 id 清单。

    不 raise：每个扩展独立容错（元数据失败 / 下载失败 / 产物缺失 → 告警跳过）。
    """
    ids = list((cfg.get("extensions") or {}).get("ids") or [])
    if not ids:
        print("无扩展配置，跳过 P2")
        return []
    dl = _download()
    build_dir = Path(build_dir)
    installed_root = Path(dist_dir) / "data" / "extensions" / "installed"
    repo = Path(extensions_repo)
    skipped: List[str] = []
    for idx, eid in enumerate(ids, start=1):
        print(f"[{idx}/{len(ids)}] {eid} ...")
        installed_dir = installed_root / eid

        # 1) 元数据 + 兼容过滤
        try:
            data = dl.get_json(f"{ZED_EXTENSIONS_API}/{eid}")
        except Exception as exc:  # noqa: BLE001 —— 网络异常走本地打包兜底
            print(f"  WARN: 官方 API 元数据获取失败（{type(exc).__name__}），走本地打包兜底：{eid}")
            skipped.extend(_fallback_package(eid, tc, repo, build_dir, installed_dir, platform))
            continue
        version = _latest_compatible_version(data.get("data") or [])
        if version is None:
            print(
                f"  WARN: {eid} 无兼容版本（schema_version>{MAX_SCHEMA_VERSION} 或 "
                f"wasm_api_version>{MAX_WASM_API_VERSION}），跳过（不走兜底）"
            )
            skipped.append(eid)
            continue

        # 2) 幂等版本比对
        toml_present, installed_version = _installed_version(installed_dir)
        if toml_present:
            if installed_version == version:
                print(f"  已是最新版 v{version}，跳过")
                continue
            if installed_version is None:
                print(f"  extension.toml 解析失败/无 version，保守视为已装，跳过：{eid}")
                continue

        # 3+4) 下载 + 落位（下载失败 → 兜底；校验失败 → 跳过不走兜底）
        try:
            ok = _install_from_api(eid, version, dl, build_dir, installed_dir)
        except dl.DownloadError as exc:
            print(f"  WARN: 官方产物下载失败（{exc}），走本地打包兜底：{eid}")
            skipped.extend(_fallback_package(eid, tc, repo, build_dir, installed_dir, platform))
            continue
        if not ok:
            skipped.append(eid)
    return skipped


def _latest_compatible_version(entries: list) -> Optional[str]:
    """过滤兼容版本（schema_version<=MAX、wasm_api_version<=MAX 或缺失）后取 semver 最大。

    返回 version 字符串；无兼容版本返回 None。
    """
    best_key: Optional[Tuple[int, int, int]] = None
    best_version: Optional[str] = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        schema = entry.get("schema_version") or 0
        try:
            schema = int(schema)
        except (TypeError, ValueError):
            schema = 0
        if schema > MAX_SCHEMA_VERSION:
            continue
        wasm_ver = entry.get("wasm_api_version")
        if wasm_ver is not None and _semver_tuple(wasm_ver) > _semver_tuple(
            MAX_WASM_API_VERSION
        ):
            continue
        version = entry.get("version")
        if not isinstance(version, str) or not version:
            continue
        key = _semver_tuple(version)
        if best_key is None or key > best_key:
            best_key, best_version = key, version
    return best_version


def _semver_tuple(value: str) -> Tuple[int, int, int]:
    """semver 字符串 → 比较元组（取前 3 数字段；预发布后缀忽略，非数字段记 0）。"""
    nums: List[int] = []
    for part in re.split(r"[.-]", value):
        nums.append(int(part) if part.isdigit() else 0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def _installed_version(installed_dir: Path) -> Tuple[bool, Optional[str]]:
    """返回 ``(toml 存在?, 顶层 version or None)``。

    解析失败或无 version 字段 → (True, None)——调用方保守视为已装。
    """
    toml_path = installed_dir / "extension.toml"
    if not toml_path.is_file():
        return False, None
    try:
        with open(toml_path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception:  # noqa: BLE001 —— 解析失败保守视为已装
        return True, None
    version = data.get("version")
    return True, version if isinstance(version, str) else None


def _install_from_api(
    eid: str, version: str, dl, build_dir: Path, installed_dir: Path
) -> bool:
    """官方 API 产物下载 → 解压 → 校验 → 原子落位。

    下载失败 raise DownloadError（调用方走兜底）；校验失败返回 False
    （不走兜底——api 产物损坏说明上游异常），并清理 installed 防坏扩展。
    """
    archive = build_dir / "ext" / "archives" / f"{eid}-{version}.tar.gz"
    dl.download_file(f"{ZED_EXTENSIONS_API}/{eid}/{version}/download", archive)
    tmp = build_dir / "ext" / "install-tmp" / eid
    if tmp.exists():
        shutil.rmtree(tmp)
    dl.extract_archive(archive, tmp)
    if not ((tmp / "extension.toml").is_file() and (tmp / "extension.wasm").is_file()):
        if installed_dir.exists():
            shutil.rmtree(installed_dir, ignore_errors=True)  # 防坏扩展
        print(f"  WARN: 官方产物缺 extension.toml/extension.wasm：{eid}（上游异常，跳过）")
        return False
    if installed_dir.exists():
        shutil.rmtree(installed_dir)
    shutil.copytree(tmp, installed_dir)
    print(f"  已安装 v{version}：{installed_dir}")
    return True


# ---------------------------------------------------------------------------
# 兜底：submodule + zed-extension 本地打包（工具链惰性获取）
# ---------------------------------------------------------------------------
def _fallback_package(
    eid: str, tc, repo: Path, build_dir: Path, installed_dir: Path, platform: str
) -> List[str]:
    """submodule 拉取 → 本地打包 → 落位；返回跳过 id 清单（0 或 1 项）。"""
    if not repo.is_dir():
        print(f"  WARN: extensions 仓库不存在：{repo}（兜底不可用，跳过 {eid}）")
        return [eid]
    if not _init_submodule(eid, repo):
        return [eid]
    out_dir = build_dir / "ext" / "out" / eid
    if not _package_extension(
        tc, eid, repo, build_dir / "ext" / "scratch", out_dir, build_dir, platform
    ):
        return [eid]
    if not _place_extension(eid, out_dir, installed_dir):
        return [eid]
    return []


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


def _package_extension(
    tc,
    eid: str,
    repo: Path,
    scratch: Path,
    out_dir: Path,
    build_dir: Path,
    platform: str,
) -> bool:
    """zed-extension 本地打包（P2 兜底）；工具链惰性：tc 字段 None/不可用时回填。"""
    from . import toolchain  # 函数内延迟导入（与 download 同风格）

    if tc.wasi_sdk_path is None or not tc.wasi_sdk_path.is_dir():
        try:
            tc.wasi_sdk_path = toolchain.ensure_wasi_sdk(build_dir, platform)
        except Exception as exc:  # noqa: BLE001 —— 惰性获取失败 → 跳过该扩展，不 raise
            print(f"  WARN: WASI SDK 惰性获取失败（{type(exc).__name__}: {exc}），跳过 {eid}")
            return False
    if tc.zed_extension is None or not (
        tc.zed_extension.is_file() and os.access(tc.zed_extension, os.X_OK)
    ):
        try:
            tc.zed_extension = toolchain.ensure_zed_extension_cli(
                build_dir, platform, tc.zed_commit
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"  WARN: zed-extension CLI 惰性获取失败（{type(exc).__name__}: {exc}），跳过 {eid}"
            )
            return False
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
