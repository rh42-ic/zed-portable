# zed-portable

[English](README.en.md) | 中文

把 Zed 打包成**离线可用的分发包**：构建时（联网环境）把运行时需要的资源全部下载好；之后在断网机器上第一次启动就能直接用，功能不缩水。

Zed 离线时会发起各种网络请求（LSP 版本查询、npm info、扩展更新检查、auto-update），这些请求都会失败，但全部回退到本地缓存。所以功能照常，个别请求会多等 ~5s 超时。

- 不修改 Zed 源码；产物就是一个 bundle：`bin/`（zed 二进制）+ `data/`（用户数据目录，经 `--user-data-dir` 指向）
- 单一入口命令 `zed-portable build`，本地与 CI 一致
- 扩展/LSP/主题由 **preset 配置文件驱动**：available/ 是候选库，`ln -s` 到 enabled/ 才生效；不链接 = 什么都不装
- 目标平台：**linux x86_64 + windows x86_64**（双平台在 CI 构建，本机仅构建/验证 linux）
- 远程服务端（P2.5）：**独立 preset `config/available/remote.toml`**，链接才启用（不链接 = bundle 不带远程服务端）；启用后默认预置 x86_64 三平台 `zed-remote-server`（linux/macos/windows）到 `data/remote_servers/`，任意平台远程（含离线）连接零下载部署；arm/aarch64 使用较少默认不装（`remote.toml` 内已注释，取消注释即可）；`[remote_server] platforms` 可裁剪（env `REMOTE_SERVER_PLATFORMS` 可独立启用）

## 直接下载（release 用户）

不想自己构建？[GitHub Releases](https://github.com/rh42-ic/zed-portable/releases) 直接下载现成 bundle：

- tag `bundle-v1.15.0` 对应 Zed v1.15.0；资产命名 `zed-portable-<Zed版本>-<平台>.<格式>`
  - `linux-x64.tar.zst`（zstd 压缩）
  - `windows-x64.7z`（7-Zip 格式）
- 包是完整版：23 个 preset 全装（扩展、LSP、主题、Node 运行时、远程服务端 x86_64 三平台），解压就能跑

linux：

```bash
tar -I zstd -xf zed-portable-v1.15.0-linux-x64.tar.zst
./run.sh
```

windows（需先安装 7-Zip）：

```powershell
7z x zed-portable-v1.15.0-windows-x64.7z
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

无 GPU 的机器首次启动若被软件渲染警告阻塞，设置 `ZED_ALLOW_EMULATED_GPU=1` 后重试。运行细节与 `--user-data-dir` 机制见下文「使用 bundle」；包内 `BUILD_INFO` 记录实际构建对账（平台、Zed tag/commit、扩展 commit、构建日期）。

## 从源码构建（开发者）

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
uv run zed-portable build
```

注意：

- `config/available/` 是**候选库**（git 管理，随工程升级），默认**不生效**；`config/enabled/`（git 忽略）才是构建输入——链接哪些 preset，构建就装什么。升级工程不会丢配置：enabled/ 里只有软链接和你自己的增量文件。
- 同一扩展被多个 preset 重复列出 → 合并时数组追加 + 字符串去重，幂等跳过。
- env 覆盖（最高优先级）：`ZED_RELEASE_TAG`、`EXTENSIONS_REV` 可显式控制产物版本，无需改配置文件。

## Preset 清单

完整清单见 [docs/PRESETS.md](docs/PRESETS.md)（23 个 preset 的用途与内容）。

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

## 限制与已知行为

- **npm 型 LSP 离线超时**：每次启动 Zed 仍会跑 `npm info`（子进程，~5s 超时后失败回退缓存），功能可用，但启动会慢一点（DESIGN.md §6）。
- **GitHub 型 LSP 离线超时**：后台查版本失败（~10s 超时）→ 回退缓存，LSP 正常启动。
- **含 LSP 的 wasm 扩展**：如 gleam/deno/latex(texlab)/zig(zls)/erlang 等，其 LSP 由扩展运行时下载到 `work/{id}/`，离线仅语法高亮；预置方式见 DESIGN.md §5.5（best-effort，不阻断构建）。
- 扩展市场离线不可用（预期）；已预置扩展纯本地加载。
- 完整离线行为对照与"尝试性请求"清单见 **DESIGN.md §6**。

## 目录结构

```
zed-portable/
├── pyproject.toml / uv.lock / .python-version   # uv 工程（python 3.12）
├── src/zed_portable/                      # 构建器（cli/config/download/.../finalize）
├── config/
│   ├── available/                               # ★候选 preset 清单库（默认不生效）
│   └── enabled/                                 # ★生效配置（ln -s preset + 自写增量；git 忽略）
├── scripts/sort_by_mime.py                      # 按 mime 重排文件列表，提升 zstd 压缩率
├── .github/workflows/build-bundle.yml           # CI 构建 + 发布
├── build/                                       # 中间产物（git 忽略）
└── dist/                                        # ★bundle 产物（git 忽略）：bin/zed + data/ + run.sh/run.ps1 + BUILD_INFO
```
