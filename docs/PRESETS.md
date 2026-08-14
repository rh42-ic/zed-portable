# Preset 清单（config/available/）

`config/available/` 是候选库，链接到 `config/enabled/` 才生效；不链接 = 不安装。下表共 23 个 preset。

| 文件名 | 分类 | 内容概要 |
|---|---|---|
| `core-zed.toml` | 核心 | Zed 本体：channel=stable、release_tag 空（解析最新 stable）、binary=download |
| `remote.toml` | 核心 | 远程开发服务端（zed-remote-server）：链接才启用，默认 x86_64 三平台（linux/macos/windows；aarch64 在文件内注释、需时取消）、source=github |
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

扩展 id 均已对照 `zed-industries/extensions` 仓库目录核验存在；`systemverilog`、`tailwind`、`rose-pine`、`yaml` 因仓库无对应目录已从清单剔除。
