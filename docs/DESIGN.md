# zed-onprem-bundle 设计方案

## 1. 目标与策略

**目标**：构建一个自包含的 Zed 离线分发包（bundle）。构建时（联网环境）下载并编译一切运行期需要的资源；运行时（离线环境）Zed 第一次启动即可找到所有东西，功能完整可用。

**策略**：不追求"严格零网络请求"，而是**预置完整 + 失败静默回退**。Zed 的所有下载点（LSP 版本查询、npm info、扩展更新检查、auto-update）在离线时均会失败，但**失败后全部回退到本地缓存**，功能不受影响（个别场景有 ~5s 超时延迟）。因此预置命中即离线可用，无需修改 Zed 源码。

**已确认决策**（用户审核）：
- 不修改 Zed 源码；独立工程；如未来需要源码级改进，以 patch 形式提供
- 扩展清单由**配置文件驱动**（构建时读取）
- 产物为**一个 bundle**：`bin/`（zed 二进制）+ `data/`（用户数据目录，通过 `--user-data-dir` 指向）
- 本机构建 Zed 本体（cargo build）：**确认默认不做**——本机 AGENTS.md 禁 rust 编译；bundle 直接复用官方 release 二进制；端到端验证如需要仅可在 CI 进行
- **实现语言：Python 3**（用户选定）。**工程结构随大流**：标准 Python 布局（pyproject.toml + src/ 包 + console script），不搞自定义薄壳/自定义目录
- **包管理与入口（用户确认）**：**全程使用 uv，禁止 pip**——python 版本（`uv python install`）、依赖安装（`uv sync`，pyproject.toml 驱动 + 提交 uv.lock）、运行（`uv run`）；入口：`uv sync` 后 `zed-onprem-bundle build`（本地与 CI 同一命令）
- **配置双目录**（用户选定）：`config/available/`（git 管理，工程随升级的常用经典配置）+ `config/enabled/`（git 忽略，用户真正启用的配置存放处，可放多个文件或软链接）；构建时扫描合并，enabled 覆盖 available
- **输出目录随大流**（用户选定）：`.gitignore` 直接采用官方 Python.gitignore（github/gitignore）；构建中间产物 → `build/`、bundle 产物 → `dist/`，均已覆盖无需专门设置
- **构建环境：GitHub Actions**（最终构建在 CI 完成，本机仅开发调试）。环境准备（setup-python、pip install、rustup target）由 workflow 显式声明，不依赖隐式本机状态
- **版本对齐（用户确认）**：bundle 版本与 Zed 上游 tag **严格对应**——release tag 命名 `bundle-<zed-tag>`；构建时版本可控制：env `ZED_RELEASE_TAG` / `EXTENSIONS_REV`（缺省解析 channel 最新 stable，实际值记录到 BUILD_INFO）
- **产物发布（用户确认）**：CI 构建完成后**直接挂 GitHub release**（tar.gz + BUILD_INFO，见 §5.7）
- **本机禁 rust 编译（AGENTS.md 硬约束）**：本机 zed-extension CLI 一律走预编译下载分支；`cargo build` 仅限 CI（见 P1）
- **目标平台（用户确认）**：**linux x86_64 + windows x86_64 双目标**——zed 二进制（`zed-linux-x86_64.tar.gz` / `Zed-x86_64.exe`）、node、LSP 均按平台下载；windows bundle 仅 CI（windows-latest）构建，本机（linux）只构建/验证 linux
- **组件选择（用户确认）：preset 清单 + 软链接**——available/ 预设多套命名清单（web 开发 / EDA 工程师 / 热门美化 等）；**要什么就 `ln -s` 到 enabled/，编译时自动合并；不链接 = 什么都不装**（扩展/LSP 默认空）
- **端到端验证（用户确认）**：仅下载官方二进制 + 离线冒烟；不做 strace 端到端（不构建 Zed 本体）
- **Zed 源码 patch（用户确认）：默认不做**（§8 仅作文档留存，接受"尝试性请求"）

## 2. 调研结论摘要（实现依据）

### 2.1 Zed 运行期下载点（root = data_dir，Linux `~/.local/share/zed`）

| # | 资源 | 来源 | 预置目标 | 运行时行为（离线时） |
|---|------|------|----------|----------------------|
| 1 | GitHub 型 LSP（rust-analyzer/clangd/ty/ruff/package-version-server） | api.github.com 最新 release | `languages/{server}/` | 后台查版本失败 → 回退缓存，可用 |
| 2 | Node v24.11.0 | nodejs.org | `node/node-v24.11.0-linux-x64/` | 判定失败会**删目录重下**——版本必须精确，校验必须通过 |
| 3 | npm 型 LSP（typescript/vtsls/yaml/css/bash/tailwind/pyright/basedpyright） | npm registry（子进程 npm info） | `languages/{dir}/node_modules/` | 每次启动 npm info 失败 → ~5s 超时 → 回退缓存，可用 |
| 4 | eslint 3.0.24 | github.com archive | `languages/eslint/vscode-eslint-3.0.24/` | 版本硬编码，server_path 存在即零网络 |
| 5 | 扩展 | api.zed.dev | `extensions/installed/{id}/` | 已安装扩展**纯本地加载**，updates 检查失败静默 |
| 6 | debugpy | pypi.org | `debug_adapters/Debugpy/` | dist-info 存在且版本==PyPI latest 即跳过；离线查版本失败 → 回退缓存（仅 warn） |
| 7 | auto-update | cloud.zed.dev | —（禁用） | `auto_update: false` 抑制 |
| 8 | telemetry / minidump | api.zed.dev / Sentry | —（禁用） | `telemetry.metrics/diagnostics: false` 抑制 |

### 2.2 关键机制

- **扩展加载纯本地**：`extensions/installed/{id}/` 目录 + `extension.toml` + `extension.wasm` 即可被加载；`index.json` 只是缓存，缺失/过期会自动重建（零网络）。预置无需写 index.json；`manifest.json` 为发布元数据（打包时生成、上传用），运行时加载不需要，可删除。
- **扩展校验**：索引时（`add_extension_to_index`，crates/extension_host/src/extension_host.rs:1640-1776）id 取自 manifest 内部字段（**目录名不被校验**）；`extension.toml` 解析失败或 `languages/` 下子目录 config.toml 非法 → 整扩展跳过；wasm 的 `zed:api-version` custom section 在 wasm 加载时校验（wasm_host.rs:807-832）。
- **GitHub 型 LSP 缓存判定**：目标二进制（`{name}-{tag}`）+ `<name>.metadata`（`{"metadata_version":1,"digest":"<sha256hex>"}`，digest 为**压缩资产字节**的 sha256）。命中需 digest 相等且 `--version` 成功。
- **npm 型 LSP 缓存判定**：server_path 文件存在 且 package.json 版本满足策略（typescript 需 `^6`，其余 latest）。预置时须装**当前 npm 最新版**（typescript 装最新 ^6.x，避开 TS 7）。
- **Node 校验**：`node/node-v24.11.0-linux-x64/bin/node` + npm `--version` 退出码 0。失败 → 删除重下。版本字符串必须精确为 `v24.11.0`。
- **构建期唯一下载**：Windows ConPTY nupkg（仅 Windows）；其余 build.rs 全为本地操作。WASI SDK v25 用于扩展打包。

## 3. 工程结构

```
zed-onprem-bundle/                 # 独立工程（本仓库）
├── pyproject.toml                  # hatchling；dependencies=[requests, tomli(<3.11 marker)]；[project.scripts] zed-onprem-bundle
├── uv.lock                         # uv sync 生成（提交，锁定依赖）
├── .python-version                 # uv 固定 python 版本（3.12）
├── README.md
├── .gitignore                      # = 官方 Python.gitignore 原文 + config/enabled/ 例外（见下）
├── docs/
│   └── DESIGN.md                     # 本文档
├── .github/
│   └── workflows/build-bundle.yml  # ★ CI 构建定义（见 §5.7）
├── src/
│   └── zed_onprem_bundle/         # 标准 src 布局包
│       ├── __init__.py
│       ├── __main__.py             # python -m zed_onprem_bundle 等价入口
│       ├── cli.py                  # argparse：build 子命令 + --config-dir 等
│       ├── config.py               # 配置合并：只扫 enabled/（展开软链接 preset）→ deep merge → 校验
│       ├── download.py             # requests 下载助手（重试/超时/代理/GitHub 限流）
│       ├── toolchain.py            # zed 二进制 + 版本解析；WASI SDK / zed-extension CLI（惰性，P2 兜底用）
│       ├── extensions.py           # 扩展：官方 API 下载（主）→ submodule 打包（兜底）→ installed/ 落位
│       ├── node.py                 # node 运行时下载 + 结构校验
│       ├── lsp_github.py           # GitHub 型 LSP：release → 下载 → 解压 → metadata
│       ├── lsp_npm.py              # npm 型 LSP 预安装 + eslint 源码编译
│       └── finalize.py             # settings.json + run.sh + 产物断言
├── config/
│   ├── available/                  # git 管理：★候选 preset 清单库（默认不生效！）
│   │   #   文件名即分类：core-*=核心 / lang-*=语言类 / 其余=领域或用途名
│   │   ├── core-zed.toml           # 核心：zed 本体（版本/二进制来源）
│   │   ├── core-node.toml          # 核心：node 运行时 + debugpy
│   │   ├── core-settings.toml      # 核心：settings.json 默认体验片段（可覆盖）
│   │   ├── web.toml                # 领域：web 开发
│   │   ├── eda.toml                # 领域：EDA 工程师（verilog/vhdl/systemverilog/tcl...）
│   │   ├── devops.toml             # 领域：devops/运维（docker/terraform/k8s/nginx...）
│   │   ├── data.toml               # 领域：数据/数据库（sql/dbt/sqlmesh...）
│   │   ├── embedded.toml           # 领域：嵌入式（arduino/platformio/openscad...）
│   │   ├── mobile.toml             # 领域：移动端（dart/flutter/swift）
│   │   ├── themes.toml             # 美化：精选主题
│   │   ├── icons.toml              # 美化：图标
│   │   ├── snippets.toml           # 美化：热门 snippets 合集
│   │   ├── misc.toml               # 其它：未分类杂项
│   │   ├── lang-python.toml        # 语言：python
│   │   ├── lang-rust.toml          # 语言：rust
│   │   ├── lang-cpp.toml           # 语言：c/c++
│   │   ├── lang-go.toml            # 语言：go
│   │   ├── lang-jvm.toml           # 语言：jvm 生态（java/kotlin/scala/groovy）
│   │   ├── lang-js-ts.toml         # 语言：js/ts
│   │   ├── lang-script.toml        # 语言：脚本（lua/perl/php/ruby/powershell）
│   │   ├── lang-functional.toml    # 语言：函数式（haskell/ocaml/elixir/erlang/clojure...）
│   │   ├── lang-sys.toml           # 语言：系统级（zig/odin/assembly/v...）
│   │   └── lang-scientific.toml    # 语言：科学计算（julia/r/matlab/typst/latex）
│   └── enabled/                    # git 忽略：★生效配置（ln -s 指向 preset + 自写增量）
│       └── .gitkeep                # 保留目录（gitignore 例外放行）
├── build/                          # 中间产物（Python.gitignore 已忽略）
└── dist/                           # ★ bundle 产物（Python.gitignore 已忽略）
    ├── bin/zed                     # Zed 二进制（linux=zed；windows=zed.exe 即 Zed-x86_64.exe 改名）
    ├── data/                       # = 运行时 data_dir，--user-data-dir 指向
    │   └── config/settings.json    # ★ 实际落位：config_dir = data_dir/config（paths.rs:124-125，两平台一致）
    ├── BUILD_INFO                  # 构建对账：平台、zed tag/commit、extensions commit、日期
    ├── run.sh                      # linux 离线启动入口
    └── run.ps1                     # windows 离线启动入口
```

**.gitignore** = 官方 Python.gitignore（https://github.com/github/gitignore/blob/main/Python.gitignore）原文，仅追加两行保留 `config/enabled/` 目录：

```gitignore
config/enabled/*
!config/enabled/.gitkeep
```

## 4. 配置：available（候选库）+ enabled（软链接生效）双目录

**不采用单个 bundle.toml**（用户选定）：单文件不利于用户跟随工程升级时保留自己的配置。改为两个目录，构建时扫描合并。

### 4.1 目录职责

| 目录 | git | 内容 | 用途 |
|---|---|---|---|
| `config/available/` | 跟踪 | **候选 preset 清单库**：多套命名清单，**文件名即分类**——`core-*` 核心（core-zed/core-node/core-settings）、`lang-*` 语言类（lang-python…lang-scientific）、其余为领域或用途名（web/eda/devops/data/embedded/mobile/themes/icons/snippets/misc），每套一个 toml，随工程升级演进 | **默认不生效**；用户从中挑选 |
| `config/enabled/` | 忽略 | **生效配置**：软链接指向 available/ 的 preset（`ln -s ../available/web.toml`），可混入用户自写 `*.toml` 增量 | 编译时合并的**唯一输入**；不链接 = 什么都不装 |

**核心规则：available/ 只是候选库，绝不自动生效。** enabled/ 里有什么，构建就装什么。
升级工程时：available/ 随仓库更新（preset 内容演进）；enabled/ 只是软链接 + 用户增量，零丢失。

### 4.2 合并规则（src/config.py）

1. **扫描**：只列 `config/enabled/*.toml`（跟随符号链接，按文件名排序）——available/ 不自动参与
2. **展开**：软链接指向 available/ 的 preset → 内容即该 preset 全文参与合并；用户自写文件原样参与（可覆盖/追加 preset 内容）
3. **顺序**：按 enabled 内文件名 ASCII 排序依次 deep-merge（用户自写增量建议 `z` 前缀命名如 `zz-mine.toml`，保证最后合并、可覆盖 preset）
4. **深度合并**：嵌套 table 递归合并（键级覆盖）；标量后者覆盖；**数组追加 + 字符串元素精确去重**（同一扩展被多个 preset 重复列出 → 幂等跳过）
5. **默认空**：enabled 为空（或目录不存在）→ 合并结果仅剩 CLI 兜底默认值（`[zed] channel=stable, binary=download`）——**不装任何扩展/LSP/node**；10-zed.toml 也需链接才有 node/debugpy
6. **校验**：合并后校验 schema（未知键告警、必需键存在、id 合法性）；打印差异摘要（每个 key 最终来源）
7. **env 覆盖（最高优先级）**：构建时 env（`ZED_RELEASE_TAG`、`EXTENSIONS_REV` 等）覆盖合并结果中的版本字段——同一配置文件下，CI/本机可显式控制产物版本

### 4.3 配置 schema（每个 preset 是独立的 toml，结构相同）

任意 preset 文件可含以下三个表（都可省略）；合并后供 P1-P6 使用：

```toml
# 例：config/available/core-zed.toml —— 核心 preset（链接它才有 node/debugpy；不链接则 [zed] 走 CLI 兜底默认）
[zed]
channel = "stable"            # stable | preview | dev
release_tag = ""              # 空=解析 channel 最新 stable；env ZED_RELEASE_TAG 覆盖（构建时可控）
binary = "download"           # download=官方 release；或 /path/to/local/zed 复用本地构建

[node]                        # 版本与 Zed 源码硬编码一致（node_runtime.rs:606），勿改
version = "v24.11.0"

[debug]
debugpy = true
```

```toml
# 例：config/available/web.toml —— 领域·web 开发 preset（扩展 id 取 extensions 仓库目录名，已核验存在）
[extensions]
rev = ""                      # extensions 仓库 commit（空=HEAD）；env EXTENSIONS_REV 覆盖
ids = [
  "vue", "svelte", "astro", "tailwind", "angular",
  "graphql", "prisma", "tsgo", "css-modules-kit",
]

[lsp.npm]                     # web 开发主要走 npm 型 LSP
typescript = true             # typescript + typescript-language-server
vtsls = true
yaml = true
css = true
bash = true
tailwind = true
pyright = true
basedpyright = false
eslint = true                 # 固定 3.0.24，源码编译
```

```toml
# 例：config/available/eda.toml —— 领域·EDA 工程师 preset（均为纯语法扩展，完全离线安全）
[extensions]
ids = [
  "verilog", "systemverilog", "vhdl", "tcl",
  "p4", "systemrdl", "bluespec-systemverilog",
  "xmake",
]
```

```toml
# 例：config/available/themes.toml —— 美化·精选主题 preset（icons.toml 图标同理）
[extensions]
ids = [
  "catppuccin", "tokyo-night", "dracula", "gruvbox-material", "nord",
  "one-dark-pro", "solarized", "everforest", "rose-pine", "kanagawa-themes",
  "file-icons", "material-icon-theme", "bearded-icons", "catppuccin-icons",
]
```

**用户启用方式**（`config/enabled/`，ln -s 即生效，可混搭多套）：

```bash
cd config/enabled
ln -s ../available/core-zed.toml     # 核心：zed 本体
ln -s ../available/core-node.toml    # 核心：node + debugpy
ln -s ../available/web.toml          # 领域：web 开发
ln -s ../available/eda.toml          # 领域：EDA
ln -s ../available/themes.toml       # 美化：主题
ln -s ../available/icons.toml        # 美化：图标
# 也可以自写增量文件（追加/覆盖 preset）：
#   vim zz-mine.toml  →  [extensions] ids = ["zig","erlang"]；[lsp.github] gopls = true
```

**CI 构建**：workflow 内同样 `ln -s` 需要的 preset 到 enabled/ 后执行 build（见 §5.7）。

## 5. 构建流水线

> 全程联网；每步幂等（产物存在则跳过）；本地与 CI 同一命令：`zed-onprem-bundle build`（`uv sync` 后）。
> 阶段编号与 `src/` 模块一一对应（cli.py 按序调用：config 合并 → P1 → P2 → P2.5 → P3..P6，单阶段失败 → 非零退出，可修复后重跑）。

### P0 环境检查
- **本机**：uv（uv python install 3.12）、node/npm、git；env：`ZED_REPO`（默认 `/home/dev/rust-dev/zed`）、`EXTENSIONS_REPO`（默认 `/home/dev/rust-dev/extensions`）、`ZED_RELEASE_TAG`/`EXTENSIONS_REV`（版本控制，优先级见 §4.2）；`uv sync`（首次；此后 `uv run zed-onprem-bundle ...`）
- **CI**（见 §5.7）：由 workflow 显式准备，脚本内仅做存在性断言（不自动安装系统包）
- **配置合并先行**（config.py）：§4 规则产出合并配置，供 P1-P6 使用
- `wasm32-wasip2` target：CI 用 `rustup target add` 显式安装；本机已装
- **平台**：`--platform linux-x64|windows-x64`（env `ZED_BUNDLE_PLATFORM` 可覆盖；缺省取本机平台）——本机 linux 只能构建 linux-x64；windows-x64 仅 CI（windows-latest）可构建

### P1 工具链（src/toolchain.py）——资产按平台
| 组件 | linux-x64 | windows-x64 | 备注 |
|---|---|---|---|
| WASI SDK v25 | `wasi-sdk-25.0-x86_64-linux.tar.gz` | `wasi-sdk-25.0-x86_64-windows.tar.gz` | **惰性**（仅 P2 兜底路径触发）：github.com/WebAssembly/wasi-sdk releases → `build/wasi-sdk`；经 `WASI_SDK_PATH` 传给 zed-extension。P2 主路径（官方 API 下载）不需要 |
| zed-extension CLI | `https://zed-extension-cli.nyc3.digitaloceanspaces.com/$SHA/x86_64-unknown-linux-gnu/zed-extension` | `.../$SHA/x86_64-pc-windows-msvc/zed-extension.exe`（**URL 待核验**） | **惰性**（仅 P2 兜底路径触发）：$SHA = ZED_RELEASE_TAG 对应 commit；**本机（linux）一律预编译下载（AGENTS.md 禁 rust 编译）**；仅 CI 可选 `cargo build -p extension_cli` 兜底（产物 `target/release/zed-extension(.exe)`）。获取失败 → 该扩展跳过（不影响 P2 主路径） |
| zed 二进制 | `zed-linux-x86_64.tar.gz`（GitHub releases 资产，release.yml:453-454） | `Zed-x86_64.exe`（单文件 exe，release.yml:723-724；复制为 `dist/bin/zed.exe`） | tag 由 `ZED_RELEASE_TAG`/`[zed] release_tag` 决定，缺省查 channel 最新；或 `binary = "/path"` 复制本地产物；下载后校验（存在 + 可执行 + `--version`） |

> 资产命名漂移对策：zed 二进制下载失败时按 release.yml:785 资产全清单（EXPECTED_ASSETS）重新解析资产名；LSP 资产同理见 P4。

### P2 扩展（src/extensions.py）
对配置中每个 id，**主路径为 Zed 官方 API 直接下载打包产物**（与 Zed 运行时安装机制一致，无需源码/编译）：

```bash
# 1. 元数据：GET https://api.zed.dev/extensions/<id> → {"data":[{version, schema_version, wasm_api_version, ...}, ...]}
#    选"最新兼容版"：schema_version(缺省0) <= 1 且 wasm_api_version(缺省兼容) <= 0.7.0，取 semver 最大
#    （常量出处：zed crates/extension_host/src/extension_host.rs:75 CURRENT_SCHEMA_VERSION=1；
#      crates/extension_host/src/wasm_host/wit.rs:60-69 stable/preview wasm_api_version_range=0.0.1..=0.7.0）
# 2. 幂等比对：installed/<id>/extension.toml 的 version == 最新兼容版 → 跳过（零下载，离线可重跑）
# 3. 下载：GET https://api.zed.dev/extensions/<id>/<version>/download
#    → 302 重定向 S3 presigned（3 分钟有效）→ tar.gz（requests 自动跟随；HEAD 会 404，勿用）
# 4. 落位：解压 → dist/data/extensions/installed/<id>/（结构 = extension.toml + extension.wasm
#    + languages/ + grammars/，无 manifest.json）；校验 extension.toml + extension.wasm 存在
```

- **兜底（api 下载失败/不可达时）**：回退源码打包——`git submodule update --init --depth 1 extensions/<id>`（EXTENSIONS_REPO 内）→ `zed-extension --scratch-dir build/ext --source-dir ... --output-dir build/ext/out/<id>` → 复制落位 + 删 manifest.json。此时惰性获取 WASI SDK + zed-extension CLI（`toolchain.ensure_wasi_sdk` / `toolchain.ensure_zed_extension_cli`），获取失败 → 该扩展跳过。
- **依赖打包顺序**：wasm 扩展的 LSP 由扩展自身在运行时下载到 `work/{id}/`（离线会失败）——含 LSP 的扩展（如 gleam）离线仅语法高亮，需后续阶段为它的 LSP 单独预置（见 §5.5）。
- **空清单语义**：合并后 `ids` 缺省/空数组 = 不装任何扩展（**不链接 = 什么都不要**）；preset 文件不写 `[extensions]` 即不装扩展
- **不在清单内的扩展不得写入** installed/（运行时 check_for_updates 只对已装扩展有效，不会删除）。
- 下载/打包失败（网络/404/编译错）→ 打印警告跳过该扩展（主路径失败先试兜底），不阻断整体。

### P2.5 远程服务端（src/remote_server.py）

**机制**：Zed 远程开发时客户端把 zed-remote-server 部署到远程机器，远程平台由**远程探测**决定（与客户端平台无关）。客户端本地缓存路径 `{data_dir}/remote_servers/{channel}/{os}-{arch}/{version}.gz`（crates/auto_update/src/auto_update.rs:591-594），只做 metadata 存在性检查即跳过下载（auto_update.rs:599）——因此本阶段**原样改名落位（零转换）**，任意平台远程（含离线）连接时客户端零下载直接部署。server 版本 = 客户端版本（= zed tag）。

| 项 | 说明 |
|---|---|
| 来源 | `zed-industries/zed` release 资产 `zed-remote-server-{os}-{arch}.{gz\|zip}`（windows=zip，linux/macos=gzip） |
| 落位 | `dist/data/remote_servers/{channel}/{os}-{arch}/{version}.gz`（channel 取 `[zed] channel`，version 取 zed tag 去 `v` 前缀——与 zed 本体对齐） |
| 配置 | **链接 `config/available/remote.toml` 才启用**（独立 preset，不跟随 core-zed；不链接 = 不下载）；`[remote_server] platforms` 可裁剪（缺省全 6）+ `source`（当前仅 github）；env `REMOTE_SERVER_PLATFORMS` 可独立启用（逗号分隔整体覆盖） |
| 跳过 | 未配置 `remote_server`（未链接 remote.toml）→ 跳过；`zed.binary` 为本地路径（zed_tag=local）→ 版本未知，告警跳过 |
| 失败语义 | 下载失败/魔数不符 → raise（远程服务端是 zed 核心功能，硬失败，区别于 P4/P5 的告警跳过） |
| 幂等 | dest 存在且魔数正确（windows=PK/zip，其余=1f8b/gzip）→ 跳过 |

### P3 Node（src/node.py）
| 平台 | 资产 | 解压后根 |
|---|---|---|
| linux-x64 | `node-v24.11.0-linux-x64.tar.xz` | `dist/data/node/node-v24.11.0-linux-x64/` |
| windows-x64 | `node-v24.11.0-win-x64.zip` | `dist/data/node/node-v24.11.0-win-x64/` |

```bash
mkdir -p dist/data/node
# 按平台下载解压 → <node_root>（上表）
mkdir -p <node_root>/cache
touch <node_root>/blank_user_npmrc <node_root>/blank_global_npmrc
# 自检（与 Zed 运行时校验相同；windows 用 node.exe + npm-cli.js 同路径逻辑）：
<node_root>/bin/node <node_root>/node_modules/npm/bin/npm-cli.js --version \
  --cache .../cache --userconfig .../blank_user_npmrc --globalconfig .../blank_global_npmrc
```
（x86_64 → x64 后缀；windows 下可执行文件为 node.exe，Zed 的 node_runtime 按平台取 `node`/`node.exe`）

### P4 GitHub 型 LSP（src/lsp_github.py）
对每个启用的 LSP：查 `api.github.com/repos/{repo}/releases` 最新稳定 tag → 下载匹配资产 → 按 Zed 期望结构落位 → 计算资产 sha256 写 metadata → `--version` 自检。

| server | repo | 资产模板（linux-x64 / windows-x64） | 落位结构 | metadata |
|---|---|---|---|---|
| rust-analyzer | rust-lang/rust-analyzer | `rust-analyzer-x86_64-unknown-linux-gnu.gz` / `rust-analyzer-x86_64-pc-windows-msvc.gz` | `languages/rust-analyzer/rust-analyzer-{tag}`（单文件） | `rust-analyzer-{tag}.metadata` |
| clangd | clangd/clangd | `clangd-linux-{tag}.zip` / `clangd-windows-{tag}.zip` | `languages/clangd/clangd_{tag}/bin/clangd(.exe)` | `clangd_{tag}/metadata`（目录内） |
| ty | astral-sh/ty | `ty-x86_64-unknown-linux-gnu.tar.gz` / `ty-x86_64-pc-windows-msvc.zip` | `languages/ty/ty-{tag}/ty-x86_64-unknown-linux-gnu/ty`（windows 同构 `ty.exe`） | `ty-{tag}.metadata` |
| ruff | astral-sh/ruff | `ruff-x86_64-unknown-linux-gnu.tar.gz` / `ruff-x86_64-pc-windows-msvc.zip` | `languages/ruff/ruff-{tag}/ruff-x86_64-unknown-linux-gnu/ruff` | `ruff-{tag}.metadata` |
| package-version-server | zed-industries/package-version-server | `package-version-server-x86_64-unknown-linux-gnu.tar.gz` / `package-version-server-x86_64-pc-windows-msvc.zip`（**windows 待核验**） | `languages/package-version-server/package-version-server-{tag}`（单文件） | **无**（目录有任意文件即缓存） |
| gopls（可选） | golang/tools | `GOBIN=... go install golang.org/x/tools/gopls@latest` | `languages/gopls/gopls_{ver}_go_{goversion}` | 无（缓存文件名须以 `gopls_` 开头） |

> **windows 资产名以实现时核验为准**：逐个试候选名（404 则按 release 页 HTML 解析资产名重试）；最终仍失败 → 告警跳过该 LSP（降级 bundle，不阻断）。

### P5 npm 型 LSP + eslint（src/lsp_npm.py）
用**预置的 node**（P3 产物）执行安装，保证与运行时同一环境：

```bash
NODE_BIN=<node_root>/bin/node       # linux；windows 为 <node_root>/node.exe
NPM=<node_root>/bin/npm             # linux；windows 为 <node_root>/npm.cmd
$NPM --prefix dist/data/languages/typescript-language-server install \
  typescript@latest typescript-language-server@latest --save-exact
```
| server | 安装目录（languages/ 下） | server_path | 备注 |
|---|---|---|---|
| typescript | `typescript-language-server` | `node_modules/typescript-language-server/lib/cli.mjs` | typescript 装最新 ^6（避开 7.x）；参数 `--stdio` |
| vtsls | `vtsls` | `node_modules/@vtsls/language-server/bin/vtsls.js` | `--stdio` |
| yaml | `yaml-language-server` | `node_modules/yaml-language-server/bin/yaml-language-server` | `--stdio` |
| css | `vscode-css-language-server` | `node_modules/vscode-langservers-extracted/bin/vscode-css-language-server` | `--stdio` |
| bash | `bash-language-server` | `node_modules/bash-language-server/out/cli.js` | 参数 `start`（非 --stdio） |
| tailwind | `tailwindcss-language-server` | `node_modules/.bin/tailwindcss-language-server` | `--stdio` |
| tailwindcss(intellisense) | `tailwindcss-intellisense-css` | `node_modules/@tailwindcss/language-server/bin/css-language-server` | `--stdio` |
| pyright | `pyright` | `node_modules/pyright/langserver.index.js` | `--stdio` |
| basedpyright | `basedpyright` | `node_modules/basedpyright/langserver.index.js` | `--stdio` |

eslint（固定 3.0.24，零网络）：
```bash
curl -L "https://github.com/microsoft/vscode-eslint/archive/refs/tags/release%2F3.0.24.tar.gz" | tar -xz
# 唯一解压目录 rename 为 vscode-eslint → 置于 languages/eslint/vscode-eslint-3.0.24/vscode-eslint/
$NPM install && $NPM run compile   # 在该仓库根
# server_path = languages/eslint/vscode-eslint-3.0.24/vscode-eslint/server/out/eslintServer.js
```

**npm 型 LSP 的离线行为预期**：每次启动仍会跑 `npm info`（子进程，5s 超时后失败回退缓存，功能可用）。这是本方案接受的唯一"尝试性请求"。若后续要消除，见 §8 patch 项。

### 5.5 含 LSP 扩展的 LSP 预置（可选，best-effort）

**机制**：wasm 扩展的 LSP 由扩展自身在运行时下载到 `extensions/work/{id}/`（wasm_host.rs:701-763；经 host 的 download_file API，路径被强制限制在 work 目录内，since_v0_8_0.rs:1063-1119）。**目录与文件名由扩展自管，无通用预置路径**，预置方式按扩展逐个推断。

**已核验分类**（extensions 仓库 1427 个扩展全部为 git submodule）：

| 扩展 | LSP 机制 | 离线可用性 |
|---|---|---|
| sql / toml / make / caddyfile | 纯语法（tree-sitter grammar） | ✅ 完全离线 |
| nix | nil/nixd 走 PATH（找不到即报错"需手动安装"，不下载） | ✅ 零网络；二进制需系统预装 |
| gleam | 运行时下载 gleam 二进制（musl 静态） | ⚠️ 需预置 |
| deno / latex(texlab) / zig(zls) / erlang(erlang-ls+elp) | 运行时 GitHub release 下载 | ⚠️ 需预置 |
| ansible | 运行时 npm 安装 @ansible/ansible-language-server | ⚠️ 需预置 |
| proto | buf/protols 下载 + protobuf-language-server（Zed 内置 wasm） | ⚠️ 2/3 需预置 |
| docker-compose / dockerfile | docker-language-server（GitHub release / npm） | ⚠️ 需预置 |

**预置方式（best-effort）**：缓存路径可从扩展源码推断（例：gleam 期望 `work/gleam/gleam-{version}/gleam`，见 crates/extension_host/src/extension_store_test.rs:979-987），手工放置同名文件即可命中缓存。无法通用化——P2 仅对含 LSP 扩展打印待办清单（id + LSP 名 + 建议方式），不阻断构建。

### P6 收尾（src/finalize.py）
1. **settings.json** → `dist/data/config/settings.json`（**关键**：设置 `--user-data-dir` 后 Zed 的 `config_dir = data_dir/config`——paths.rs:124-125，settings_file 读 config_dir/settings.json——paths.rs:280；放 `data/` 根目录不生效）：
   ```json
   { "telemetry": { "metrics": false, "diagnostics": false }, "auto_update": false }
   ```
2. **启动脚本**：`dist/run.sh`（linux）+ `dist/run.ps1`（windows）：
   ```bash
   #!/usr/bin/env bash        # run.sh
   exec "$(dirname "$(readlink -f "$0")")/bin/zed" \
        --user-data-dir "$(dirname "$(readlink -f "$0")")/data" "$@"
   ```
   ```powershell
   # run.ps1
   $root = Split-Path -Parent $MyInvocation.MyCommand.Path
   & (Join-Path $root "bin\zed.exe") --user-data-dir (Join-Path $root "data") @args
   ```
3. **产物校验清单**（存在性断言，失败即报错退出；按平台取可执行名）：
   - `<node_root>/bin/node`（windows：`node.exe`）
   - 每个启用的 github 型 LSP 目标文件 + metadata
   - 每个 npm 型 LSP server_path
   - `data/extensions/installed/<id>/extension.wasm`（每个配置的扩展）
4. **BUILD_INFO** → `dist/BUILD_INFO`：bundle 版本（= zed tag）、zed commit、extensions commit、构建日期、启用的配置文件列表。
5. 打印 bundle 总大小 + 各组件大小。

### P7 GitHub Actions 工作流（.github/workflows/build-bundle.yml）

最终构建在 CI 完成；本机仅开发调试。workflow 只负责**环境准备**，业务逻辑全部走与本地相同的命令 `zed-onprem-bundle build`（保证可复现）。

```yaml
name: build-bundle
on:
  workflow_dispatch:        # 手动触发
  push:
    tags: ["bundle-*"]      # 打 tag 即发布构建
jobs:
  build-linux:
    runs-on: ubuntu-latest
    steps: [同下，平台参数 ZED_BUNDLE_PLATFORM=linux-x64，产物 zed-onprem-bundle-linux-x64]
  build-windows:
    runs-on: windows-latest
    steps: [同下，平台参数 ZED_BUNDLE_PLATFORM=windows-x64，产物 zed-onprem-bundle-windows-x64]
  # 两 job 共用的步骤模板（bundle 工程内保留一份带注释的完整 yaml）：
  #   checkout bundle + extensions + zed 三仓库（同现有）
  #   astral-sh/setup-uv → uv sync --project ./bundle
  #   setup-node 22（linux 构建 npm 依赖用；windows 同）
  #   dtolnay/rust-toolchain@stable targets wasm32-wasip2（仅 windows job 需要？——不，
  #   zed-extension CLI 默认走预编译下载，cargo build 仅作兜底，两平台都保留）
  #   可执行 build 前先 ln -s 所需 preset（git 忽略 enabled/，CI 每次重建）：
  #     cd bundle/config/enabled && ln -s ../available/core-zed.toml ../available/core-node.toml \
  #       ../available/web.toml ../available/eda.toml ../available/themes.toml
  #   Resolve bundle version（GITHUB_REF_NAME#bundle-，tag 触发才设 ZED_RELEASE_TAG）
  #   zed-onprem-bundle build（env：ZED_REPO/EXTENSIONS_REPO/ZED_RELEASE_TAG/GITHUB_TOKEN/
  #     WASI_SDK_VERSION=25/ZED_BUNDLE_PLATFORM=<平台>）
  #   upload-artifact（name 按平台；if-no-files-found: error）
  release:
    needs: [build-linux, build-windows]
    if: startsWith(github.ref, 'refs/tags/bundle-')   # 仅 tag 触发发布
    runs-on: ubuntu-latest
    permissions:
      contents: write                       # gh release 需要
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: zed-onprem-bundle-linux-x64
          path: bundle-dist-linux
      - uses: actions/download-artifact@v4
        with:
          name: zed-onprem-bundle-windows-x64
          path: bundle-dist-windows
      - name: Package
        shell: bash
        run: |
          tar -C bundle-dist-linux -czf zed-onprem-bundle-${GITHUB_REF_NAME#bundle-}-linux-x64.tar.gz .
          (cd bundle-dist-windows && zip -r ../zed-onprem-bundle-${GITHUB_REF_NAME#bundle-}-windows-x64.zip .)
      - name: Create GitHub release          # gh CLI（runner 预装），无需第三方 action
        run: |
          gh release create "$GITHUB_REF_NAME" zed-onprem-bundle-*.tar.gz zed-onprem-bundle-*.zip \
            --title "zed-onprem-bundle ${GITHUB_REF_NAME#bundle-}" \
            --notes "离线 bundle，对应 Zed ${{ github.ref_name }}（详见产物内 BUILD_INFO）"
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

要点：
- **环境显式声明**：uv 管理 python 3.12（`uv python install 3.12`，tomllib 可用）、node 22、rust stable + wasm32-wasip2、WASI SDK 由 P1 下载——不依赖 runner 隐式状态
- **单一命令入口**：CI 与本地均为 `zed-onprem-bundle build`（uv sync 后，全程 uv 禁止 pip）；**CI 无 `config/enabled/`（git 忽略）→ 默认空 bundle，workflow 需先 `ln -s` 所需 preset 到 enabled/**（例：`core-zed.toml` + `core-node.toml` + `web.toml` + `eda.toml`）；平台经 `ZED_BUNDLE_PLATFORM` 指定（linux job / windows job 各设各的）
- **双平台产物**：linux → tar.gz；windows → zip（release 双资产，均含 BUILD_INFO）
- **版本控制**：tag `bundle-<zed-tag>` 推送 → workflow 解析出 `ZED_RELEASE_TAG` 传入构建，产物与上游 tag 严格对应；手动触发 → 空 = 最新 stable（构建后记录到 BUILD_INFO）
- **发布**：release job 仅在 tag 推送时运行：tar.gz 打包 → `gh release create`（gh CLI runner 预装；permissions.contents: write）；workflow_dispatch 只出 artifact
- **扩展 submodule**：extensions 仓库由 actions/checkout 全量拉取（bundle 工程内不需要 submodule；P2 在 EXTENSIONS_REPO 内做 `git submodule update --init`，网络在 CI 可用）
- **GITHUB_TOKEN** 自动注入：GitHub API 限流（无 token 60 req/h，CI 有 token 1000/h）
- **产物**：upload-artifact 导出 dist/；**tag 推送时自动打 tar.gz 挂 GitHub release**（§5.7 release job）
- **Zed 本体二进制**：默认 `binary = "download"`（CI 拉官方 release，几秒完成）；若需验证本地构建版，另加 `cargo build --release -p zed` job（耗时 ~30min，默认不做）

## 6. 运行时离线行为对照

| 场景 | 离线时表现 |
|---|---|
| 启动 | 扩展 updates 检查失败静默；auto-update 被配置禁用；telemetry 被配置禁用；无登录不连 collab |
| 打开文件（github 型 LSP） | 后台查 release 失败（~10s 超时）→ 回退缓存，LSP 正常启动 |
| 打开文件（npm 型 LSP） | npm info 失败（~5s 超时）→ 回退缓存，LSP 正常启动 |
| 扩展使用 | 已预置扩展正常加载；扩展市场不可用（预期） |
| node 校验 | 预置目录结构精确匹配，判定通过，零网络 |
| eslint | server_path 存在，零网络 |

**已知的"尝试性请求"**（功能不受影响）：LSP 版本查询（github 型每次启动会话一次）、npm info（npm 型每次启动）、`languages/` 之外无其他隐藏下载。

## 7. 验证方案

1. **产物自检**（P6 内建）：全部文件存在性断言 + node/LSP `--version` 冒烟。
2. **扩展冒烟**（离线）：构建后 `git stash` 掉联网路径，用 dist/run.sh 启动，确认：预置扩展出现在扩展面板、语法高亮生效、LSP 可启动（如 rust-analyzer 打开 .rs）。
3. **端到端（已确认：不做 strace）**：仅"下载官方二进制 + 离线冒烟"——构建产物在离线环境用 run.sh 启动，确认扩展面板/高亮/LSP 可用（§7 第 2 条）。不做 Zed 本体构建（本机禁编译、CI 不加 cargo build zed job）。
4. **重复构建**：二次运行 `zed-onprem-bundle build` 应全部命中缓存（幂等验证）。

## 8. 可选后续（需改 Zed 源码时，以 patch 提供）

> **已确认：默认不做**（仅作文档留存；当前接受 §6 的"尝试性请求"）。

仅当"尝试性请求"不可接受时才需要：
1. `main.rs:508-514` 现为 `ReqwestClient::proxy_and_user_agent`，换 `BlockedHttpClient`（定义于 http_client.rs:379，已有先例 node_runtime.rs:79）或加 `ZED_OFFLINE` 环境变量短路
2. 暴露 `allow_binary_download` 设置（main.rs:537 有 TODO）
3. `language.rs:768` 缓存命中时跳过 `fetch_latest_server_version`
4. npm 子进程加 `--offline` 参数

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| 扩展打包依赖 submodule 未初始化 | P2 显式 `git submodule update --init`；失败跳过并告警 |
| 含 LSP 的 wasm 扩展离线缺 LSP | 清单优先纯语法扩展；gleam 类扩展需手工预置其 LSP 二进制到 `work/{id}/`（P2 打印待办清单，见 §5.5） |
| GitHub API 限流（无 token 60/h） | 脚本支持 `GITHUB_TOKEN` 环境变量；资产 URL 可从 release 页 HTML 解析作降级 |
| node 版本号漂移 | 版本写死在 bundle.toml 并注释"与 Zed 源码 node_runtime.rs:606 同步" |
| typescript 装到 7.x 被 Zed 判定不匹配 | 安装时按 `^6` 约束（`npm install typescript@6` 解析最新 6.x） |
| bundle 体积（node ~60MB + LSP + 扩展） | 清单按需配置；打印体积报告 |
| 官方 zed release 二进制漂移/不可用 | `binary = "download"` 时固定 channel + 下载后校验（文件存在 + 可执行 + `--version`）；失败可回退本地构建产物 |
| zed release 资产命名漂移 | 资产名 `zed-linux-x86_64.tar.gz`/`zed-linux-aarch64.tar.gz`（已核验 zed/.github/workflows/release.yml:453-454）；下载后 `--version` 校验兜底 |
| 离线机无 GPU | 软件渲染警告阻塞启动（zed.rs:734-756）→ run.sh 文档注明可 `ZED_ALLOW_EMULATED_GPU=1` 绕过；bundle 不预置 |
| CI 超时（扩展打包、cargo build） | preset 按需（典型规模 5-30 扩展 × 1-3s，分钟级）；扩展失败不阻断（告警跳过）；阶段幂等可续跑；zed-extension 优先下载 CI 预编译版；全量 cargo build 默认不做 |
| windows 资产名待核验（zed-extension CLI / package-version-server 等） | 候选名逐一尝试 + release 页 HTML 解析降级；最终失败 → 告警跳过（CLI 仅影响 P2 兜底路径的该扩展；package-version-server 降级 bundle）；CI windows job 实测首跑时修正 |
| preset 误收装饰类扩展 | 预设内容工程维护（人工把关）；用户可 ln -s 自写增量覆盖或去链接；打包失败自动跳过不阻断 |
| extensions 仓库 submodule 网络量大 | 仅 P2 兜底路径（api 下载失败）触发，且只 `git submodule update --init --depth 1 extensions/<id>` 清单内扩展 |
| CI 与本地行为不一致 | 单一入口命令 `zed-onprem-bundle build`；workflow 只做环境准备；产物断言（P6）在两端都执行 |
