"""命令行入口：zed-portable build（argparse）。

流水线阶段（P1-P6）模块按序惰性 import 并调用——模块缺失/未就绪时不
影响 --help / --version（并行 lane 的模块就绪后即可用）。
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .config import load_merged_config

log = logging.getLogger("zed_portable")

#: 工程根（pyproject 所在目录）：src/zed_portable/cli.py → parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_EXTENSIONS_REPO = "/home/dev/rust-dev/extensions"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zed-portable",
        description="Self-contained offline Zed distribution bundle builder",
    )
    parser.add_argument("--version", action="store_true", help="Show version and exit")
    sub = parser.add_subparsers(dest="command", metavar="<command>", help="subcommands")
    build = sub.add_parser("build", help="Run the full build pipeline (P1-P6)")
    build.add_argument(
        "--config-dir",
        default=str(PROJECT_ROOT / "config"),
        help="Config file directory (default %(default)s)",
    )
    build.add_argument(
        "--build-dir",
        default=str(PROJECT_ROOT / "build"),
        help="Intermediate artifact directory (default %(default)s)",
    )
    build.add_argument(
        "--dist-dir",
        default=str(PROJECT_ROOT / "dist"),
        help="Bundle output directory (default %(default)s)",
    )
    build.add_argument(
        "--platform",
        choices=("linux-x64", "windows-x64"),
        default=None,
        help="Target platform (overridden by env ZED_BUNDLE_PLATFORM; defaults to local platform)",
    )
    return parser


def resolve_platform(arg: str | None) -> str:
    """平台优先级：--platform → env ZED_BUNDLE_PLATFORM → 本机平台。"""
    value = arg or os.environ.get("ZED_BUNDLE_PLATFORM") or (
        "windows-x64" if sys.platform == "win32" else "linux-x64"
    )
    if value not in ("linux-x64", "windows-x64"):
        raise SystemExit(f"invalid platform: {value} (expected linux-x64 or windows-x64)")
    return value


def _run_stage(label: str, module: str, func: str, *args) -> tuple[bool, object]:
    """惰性 import 并调用一个流水线阶段。

    异常 → 打印阶段失败，返回 (False, None)；单阶段失败非零退出，可修复后重跑。
    """
    log.info("== %s ==", label)
    try:
        mod = importlib.import_module(f"zed_portable.{module}")
        return True, getattr(mod, func)(*args)
    except Exception as exc:  # noqa: BLE001 —— 阶段失败统一拦截
        log.error("stage failed: %s -- %s: %s", label, type(exc).__name__, exc)
        return False, None


def cmd_build(args: argparse.Namespace) -> int:
    config_dir = Path(args.config_dir)
    build_dir = Path(args.build_dir)
    dist_dir = Path(args.dist_dir)
    platform = resolve_platform(args.platform)
    log.info("project root: %s", PROJECT_ROOT)
    log.info("platform: %s", platform)

    # P0 配置合并先行（§4 规则）
    try:
        cfg = load_merged_config(config_dir)
    except Exception as exc:  # noqa: BLE001
        log.error("stage failed: P0 config merge -- %s: %s", type(exc).__name__, exc)
        return 1

    enabled_dir = config_dir / "enabled"
    config_files = (
        sorted(p.name for p in enabled_dir.glob("*.toml")) if enabled_dir.is_dir() else []
    )
    extensions_repo = os.environ.get("EXTENSIONS_REPO", DEFAULT_EXTENSIONS_REPO)

    ok, tc = _run_stage(
        "P1 toolchain", "toolchain", "ensure_zed_binary",
        cfg, platform, build_dir, dist_dir,
    )
    if not ok:
        return 1
    ok, skipped = _run_stage(
        "P2 extensions", "extensions", "build_extensions",
        cfg, tc, platform, build_dir, dist_dir, extensions_repo,
    )
    if not ok:
        return 1
    ok, _ = _run_stage(
        "P2.5 remote server", "remote_server", "ensure_remote_servers",
        cfg, tc.zed_tag, dist_dir / "data",
    )
    if not ok:
        return 1
    ok, np = _run_stage(
        "P3 Node", "node", "ensure_node", cfg, platform, dist_dir,
    )
    if not ok:
        return 1
    ok, failed_gh = _run_stage(
        "P4 GitHub LSP", "lsp_github", "install_github_lsps",
        cfg, platform, dist_dir,
    )
    if not ok:
        return 1
    ok, failed_npm = _run_stage(
        "P5 npm LSP", "lsp_npm", "install_npm_lsps",
        cfg, np, platform, dist_dir,
    )
    if not ok:
        return 1
    ok, _ = _run_stage(
        "P6 finalize", "finalize", "finalize",
        cfg, platform, dist_dir, tc, np, failed_gh, failed_npm, skipped, config_files,
    )
    if not ok:
        return 1

    print(f"bundle complete: {dist_dir}")
    return 0


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        sys.exit(0)
    if args.command == "build":
        sys.exit(cmd_build(args))
    parser.print_help()
    sys.exit(0)
