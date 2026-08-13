"""配置合并（§4.2 规则 1-7）：只扫 config/enabled/*.toml → deep merge → 校验 → env 覆盖。

产物结构约定（下游模块消费）：
- 顶层键：zed / node / debug / extensions / lsp / settings
- lsp.github / lsp.npm 各为 {server_name: bool}
- settings 为任意嵌套 dict（最终并入 settings.json）
- extensions: rev(str), ids(list[str])；node: version(str)；debug: debugpy(bool)
- 返回 dict 含 "_sources"（顶层键 ← 来源文件列表）
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Mapping

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib

log = logging.getLogger("zed_onprem_bundle.config")

#: 兜底默认（enabled 为空或目录不存在时——不装任何扩展/LSP/node）
DEFAULT_CONFIG: dict = {
    "zed": {"channel": "stable", "release_tag": "", "binary": "download"},
}

#: 已知顶层键（§4.3 schema）
KNOWN_TOP_KEYS = {"zed", "node", "debug", "extensions", "lsp", "settings"}

#: env 覆盖映射：env 变量 → (顶层键, 子键)；ZED_BUNDLE_PLATFORM 由 cli 处理
_ENV_OVERRIDES = [
    ("ZED_RELEASE_TAG", ("zed", "release_tag")),
    ("EXTENSIONS_REV", ("extensions", "rev")),
]


def load_merged_config(config_dir: Path, env: Mapping | None = None) -> dict:
    """合并 config_dir/enabled/*.toml，返回含 "_sources" 的最终配置 dict。

    - 只扫 enabled/*.toml（跟随软链接，直接读内容）；available/ 不自动参与
    - 按文件名 ASCII 排序，逐个 deep-merge（后合并者优先）
    - env 覆盖（非空才生效）最后应用，优先级最高
    """
    if env is None:
        env = os.environ
    config_dir = Path(config_dir)
    enabled_dir = config_dir / "enabled"
    files = sorted(enabled_dir.glob("*.toml")) if enabled_dir.is_dir() else []

    merged = copy.deepcopy(DEFAULT_CONFIG)
    sources: dict[str, list[str]] = {}

    for path in files:
        data = _parse_toml(path)
        for key, value in data.items():
            if key == "_sources":
                continue
            if key not in KNOWN_TOP_KEYS:
                log.warning("配置 %s：未知顶层键 %r，忽略", path.name, key)
                continue
            _record(sources, key, path.name)
            if isinstance(value, dict):
                current = merged.get(key)
                if not isinstance(current, dict):
                    merged[key] = {}
                _deep_merge(merged[key], value)
            else:
                merged[key] = value

    # 兜底默认始终参与，来源标记补记
    for key in DEFAULT_CONFIG:
        _record(sources, key, "defaults")

    _validate(merged)
    _apply_env_overrides(merged, env)

    merged["_sources"] = sources
    _log_sources(merged, sources)
    return merged


# ---------------------------------------------------------------------------
# 合并原语
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, overlay: dict) -> dict:
    """深度合并：嵌套 dict 递归键级覆盖；list 追加 + 字符串精确去重（保序）；标量后者覆盖。"""
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        elif isinstance(value, list):
            existing = base.get(key)
            if isinstance(existing, list):
                base[key] = _merge_lists(existing, value)
            else:
                base[key] = _merge_lists([], value)  # 首次赋值也归一化去重
        else:
            base[key] = value
    return base


def _merge_lists(base: list, overlay: list) -> list:
    """追加并精确去重字符串元素（保序）；非字符串元素直接追加。"""
    result = list(base)
    seen = {item for item in base if isinstance(item, str)}
    for item in overlay:
        if isinstance(item, str):
            if item in seen:
                continue
            seen.add(item)
        result.append(item)
    return result


def _record(sources: dict[str, list[str]], key: str, source: str) -> None:
    lst = sources.setdefault(key, [])
    if source not in lst:
        lst.append(source)


# ---------------------------------------------------------------------------
# 解析 / 校验 / env 覆盖
# ---------------------------------------------------------------------------


def _parse_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"无法解析配置文件 {path}: {exc}") from exc


def _validate(merged: dict) -> None:
    # extensions.ids 必须为 str 列表
    ext = merged.get("extensions")
    if ext is None:
        ext = merged["extensions"] = {}
    if not isinstance(ext, dict):
        raise ValueError("extensions 必须为 table")
    ids = ext.get("ids", [])
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError("extensions.ids 必须为字符串列表")

    # lsp.github / lsp.npm 值为 bool；非 bool 忽略 + 告警
    lsp = merged.get("lsp")
    if lsp is None:
        lsp = merged["lsp"] = {}
    if not isinstance(lsp, dict):
        raise ValueError("lsp 必须为 table")
    for group in ("github", "npm"):
        table = lsp.get(group)
        if table is None:
            continue
        if not isinstance(table, dict):
            log.warning("lsp.%s 非 table，忽略", group)
            del lsp[group]
            continue
        for name, flag in list(table.items()):
            if not isinstance(flag, bool):
                log.warning("lsp.%s.%s = %r 非 bool，忽略", group, name, flag)
                del table[name]

    # 结构规范化（合并后的完整形态）：下游模块可无 .get 兜底直接访问
    ext.setdefault("rev", "")
    ext.setdefault("ids", [])
    for group in ("github", "npm"):
        lsp.setdefault(group, {})


def _apply_env_overrides(merged: dict, env: Mapping) -> None:
    """env 覆盖（最高优先级，最后应用）；仅当 env 值非空才覆盖。"""
    for var, (top, sub) in _ENV_OVERRIDES:
        value = env.get(var)
        if not value:
            continue
        if not isinstance(merged.get(top), dict):
            merged[top] = {}
        merged[top][sub] = value


def _log_sources(merged: dict, sources: dict[str, list[str]]) -> None:
    log.info("配置来源摘要（顶层键 ← 文件）：")
    for key in merged:
        if key == "_sources":
            continue
        log.info("  %-12s ← %s", key, "、".join(sources.get(key, []) or ["（无）"]))
