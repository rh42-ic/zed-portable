# zed-onprem-bundle

自包含的 **Zed 离线分发包**构建器。构建时（联网环境）下载并预置一切运行期资源；运行时（离线环境）Zed 第一次启动即可找到所有东西，功能完整可用。

策略一句话：**预置完整 + 失败静默回退**——Zed 的所有下载点（LSP 版本查询、npm info、扩展更新检查、auto-update）在离线时均会失败，但失败后全部回退到本地缓存，功能不受影响（个别场景有 ~5s 超时延迟）。

- 不修改 Zed 源码；产物为一个 bundle：`bin/`（zed 二进制）+ `data/`（用户数据目录，经 `--user-data-dir` 指向）
- 单一入口命令 `zed-onprem-bundle build`，本地与 CI 一致
- 扩展/LSP/主题由 **preset 配置文件驱动**：available/ 是候选库，`ln -s` 到 enabled/ 才生效；不链接 = 什么都不装
- 目标平台：**linux x86_64 + windows x86_64**（双平台在 CI 构建，本机仅构建/验证 linux）
- 远程服务端（P2.5）：**独立 preset `config/available/remote.toml`**，链接才启用（不链接 = bundle 不带远程服务端）；启用后预置全 6 平台 `zed-remote-server`（linux/macos/windows × x86_64/aarch64）到 `data/remote_servers/`，任意平台远程（含离线）连接零下载部署；`[remote_server] platforms` 可裁剪（env `REMOTE_SERVER_PLATFORMS` 可独立启用）

## 快速开始

前提：本机已安装 [uv](https://docs.astral.sh/uv/)（uv 负责 python 3.12、依赖同步与命令运行）。

```bash
# 1. 同步依赖（首次；由 pyproject.toml + 提交的 uv.lock 驱动）
uv sync

# 2. 选择组件：把想要的 preset 链接到 enabled/（软链接即生效）
cd config/enabled
ln -s ../available/core-zed.toml     # 核心：zed 本体
ln -s ../available/core-node.toml    # 核心：node + debugpy
ln -s ../available/web.toml          # 领域：web 开发
ln -s ../available/lang-rust.toml    # 语言：rust
ln -s ../available/themes.toml       # 美化：主题
# 也可以自写增量文件（追加/覆盖 preset；建议 z 前缀保证最后合并）：
#   vim zz-mine.toml
#   [extensions]
#   ids = ["zig", "erlang"]
#   [lsp.github]
#   gopls = true

# 3. 构建（输出到 dist/）
cd ..
uv run zed-onprem-bundle build
```

要点：

- `config/available/` 是**候选库**（git 管理，随工程升级），默认**不生效**；`config/enabled/`（git 忽略）才是构建输入——链接哪些 preset，构建就装什么。升级工程时 enabled/ 只是软链接 + 用户增量，零丢失。
- 同一扩展被多个 preset 重复列出 → 合并时数组追加 + 字符串去重，幂等跳过。
- env 覆盖（最高优先级）：`ZED_RELEASE_TAG`、`EXTENSIONS_REV` 可显式控制产物版本，无需改配置文件。

## Preset 清单（config/available/）

| 文件名 | 分类 | 内容概要 |
|---|---|---|
| `core-zed.toml` | 核心 | Zed 本体：channel=stable、release_tag 空（解析最新 stable）、binary=download |
| `remote.toml` | 核心 | 远程开发服务端（zed-remote-server）：链接才启用，预置全 6 平台（platforms 可裁剪）、source=github |
| `core-node.toml` | 核心 | Node v24.11.0 运行时（与 Zed 源码 node_runtime.rs:606 同步，勿改）+ debugpy |
| `core-settings.toml` | 核心 | settings.json 默认体验片段：One Dark / tab_size=4 / autosave=on_focus_change（可覆盖） |
| `web.toml` | 领域 | web 开发：vue/svelte/astro/angular/graphql/prisma/tsgo/css-modules-kit + npm 型 LSP 全套（typescript/vtsls/yaml/css/bash/tailwind/pyright/eslint） |
| `eda.toml` | 领域 | EDA 工程师：verilog/vhdl/tcl/p4/systemrdl/bluespec-systemverilog/xmake（纯语法，完全离线安全） |
| `devops.toml` | 领域 | devops/运维：docker-compose/dockerfile/terraform/opentofu/tflint/helm/nginx/github-actions/hurl/jq/kubernetes-snippets/taskfile |
| `data.toml` | 领域 | 数据/数据库：sql/csv/dbt/sqlmesh/dbml/snowflake |
| `embedded.toml` | 领域 | 嵌入式：arduino/platformio/openscad/pioasm/linkerscript |
| `mobile.toml` | 领域 | 移动端：dart/flutter-snippets/swift |
| `themes.toml` | 美化 | 精选主题：catppuccin/tokyo-night/dracula/gruvbox-material/nord/one-dark-pro/solarized/everforest/kanagawa-themes/ayu-theme/material-theme |
| `icons.toml` | 美化 | 图标：file-icons/material-icon-theme/bearded-icons/catppuccin-icons/vscode-icons/jetbrains-icons/ferret/colored-zed-icons-theme |
| `snippets.toml` | 美化 | 热门 snippets：javascript/typescript/react/html/python/rust/go snippets + emmet + turbo-log |
| `misc.toml` | 其它 | 杂项：caddyfile/make/toml/just/xml/json5/editorconfig/d2/mermaid/plantuml/cspell/markdownlint/ltex |
| `lang-python.toml` | 语言 | python：django/django-snippets/pyrefly/pylsp/fastapi-flask-snippets + ruff(GitHub) + pyright(npm) |
| `lang-rust.toml` | 语言 | rust：cargo-tom/rust-snippets/rust-workflow-snippets + rust-analyzer(GitHub) |
| `lang-cpp.toml` | 语言 | c/c++：neocmake/cpp2 + clangd(GitHub) |
| `lang-go.toml` | 语言 | go：go-snippets/golangci-lint；gopls 默认关闭（需 go 工具链） |
| `lang-jvm.toml` | 语言 | jvm 生态：java/kotlin/scala/groovy |
| `lang-js-ts.toml` | 语言 | js/ts 纯语言栈：tsgo/ts-macro/tsrx + typescript/vtsls(npm) |
| `lang-script.toml` | 语言 | 脚本：lua/perl/php/ruby/powershell |
| `lang-functional.toml` | 语言 | 函数式：haskell/ocaml/elixir/erlang/clojure/fsharp/lean4/racket/scheme/koka |
| `lang-scientific.toml` | 语言 | 科学计算：julia/r/matlab/typst/latex/quarto |

> 扩展 id 均已对照 `zed-industries/extensions` 仓库目录核验存在；`systemverilog`、`tailwind`、`rose-pine`、`yaml` 因仓库无对应目录已从清单剔除。

## 使用 bundle（dist/）

```bash
# linux
./dist/run.sh                 # 等价于 bin/zed --user-data-dir ./dist/data "$@"
# windows
.\dist\run.ps1
```

- **`--user-data-dir` 机制**：Zed 将 `data/` 作为运行时数据目录（`config_dir = data_dir/config`），settings.json 落在 `data/config/settings.json`；预置的扩展/LSP/node 全部在此目录下，离线首次启动即命中缓存。
- **无 GPU 机器**：软件渲染警告可能阻塞启动，可设 `ZED_ALLOW_EMULATED_GPU=1` 绕过（bundle 不预置 GPU 相关资源）。
- **BUILD_INFO**：`dist/BUILD_INFO` 记录构建对账——平台、zed tag/commit、extensions commit、构建日期、启用的配置文件列表，发布时随产物一起提供。

## 版本控制

- release tag 命名 **`bundle-<zed-tag>`**（如 `bundle-v0.180.0`），与 Zed 上游 tag 严格对应。
- 构建时版本可控：`ZED_RELEASE_TAG`（zed tag，缺省解析 channel 最新 stable）/ `EXTENSIONS_REV`（extensions 仓库 commit，缺省 HEAD），优先级高于配置文件；实际值记录到 BUILD_INFO。
- 同一配置文件下，CI 与本机可分别用 env 显式控制产物版本。

## CI 发布流程

`.github/workflows/build-bundle.yml`：

1. **workflow_dispatch**（手动）→ 构建 linux-x64 + windows-x64 双平台产物，上传 artifact（不发布）。
2. **push tag `bundle-*`** → 构建后 release job 自动打包（linux tar.gz + windows zip，均含 BUILD_INFO）并 `gh release create` 挂 GitHub release。
3. CI 每次重建 `config/enabled/`（git 忽略）：workflow 内 `ln -s` preset 后执行与本地相同的 `uv sync` → `zed-onprem-bundle build`；全程 uv，禁止 pip。

## 限制与已知行为

- **npm 型 LSP 离线超时**：每次启动 Zed 仍会跑 `npm info`（子进程，~5s 超时后失败回退缓存），功能可用，但有感知延迟（DESIGN.md §6）。
- **GitHub 型 LSP 离线超时**：后台查版本失败（~10s 超时）→ 回退缓存，LSP 正常启动。
- **含 LSP 的 wasm 扩展**：如 gleam/deno/latex(texlab)/zig(zls)/erlang 等，其 LSP 由扩展运行时下载到 `work/{id}/`，离线仅语法高亮；预置方式见 DESIGN.md §5.5（best-effort，不阻断构建）。
- 扩展市场离线不可用（预期）；已预置扩展纯本地加载。
- 完整离线行为对照与"尝试性请求"清单见 **DESIGN.md §6**。

## 目录结构

```
zed-onprem-bundle/
├── pyproject.toml / uv.lock / .python-version   # uv 工程（python 3.12）
├── src/zed_onprem_bundle/                      # 构建器（cli/config/download/.../finalize）
├── config/
│   ├── available/                               # ★候选 preset 清单库（默认不生效）
│   └── enabled/                                 # ★生效配置（ln -s preset + 自写增量；git 忽略）
├── .github/workflows/build-bundle.yml           # CI 构建 + 发布
├── build/                                       # 中间产物（git 忽略）
└── dist/                                        # ★bundle 产物（git 忽略）：bin/zed + data/ + run.sh/run.ps1 + BUILD_INFO
```
