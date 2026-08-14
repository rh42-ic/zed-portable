# zed-portable

English | [中文](README.md)

Build a self-contained offline bundle for [Zed](https://zed.dev). The build runs on a networked machine and downloads every runtime resource in advance. The bundle then runs on an offline machine with full functionality on first launch.

Strategy: **pre-provision everything, fall back silently when a fetch fails**. Every download point in Zed (LSP version lookup, `npm info`, extension update check, auto-update) fails offline, and each one falls back to the local cache. Functionality is unaffected; a few lookups add a ~5s timeout delay.

What you get:

- No changes to Zed source. The bundle is `bin/` (the zed binary) plus `data/` (the user data directory, wired in via `--user-data-dir`).
- One entry point: `zed-portable build`. Local builds and CI run the same command.
- Extensions, LSPs, and themes are driven by **preset config files**: `available/` is the candidate library; a preset only takes effect when you `ln -s` it into `enabled/`. No link, nothing installed.
- Target platforms: **linux x86_64 + windows x86_64**, both built in CI. Local builds cover linux only.
- Remote server (P2.5): a separate preset `config/available/remote.toml`. Enabled only when linked. When enabled it pre-provisions `zed-remote-server` for the three x86_64 platforms (linux/macos/windows) into `data/remote_servers/`, so connecting to any of those remotes offline deploys with zero downloads. arm/aarch64 is commented out inside the file; uncomment to include. Trim platforms with `[remote_server] platforms`, or enable independently via the `REMOTE_SERVER_PLATFORMS` env var.

## Download and run (release users)

Don't want to build it yourself? Grab a ready-made bundle from [GitHub Releases](https://github.com/rh42-ic/zed-portable/releases):

- Tag `bundle-v1.15.0` maps to Zed v1.15.0. Assets are named `zed-portable-<zed-version>-<platform>.<format>`:
  - `linux-x64.tar.zst` (zstd)
  - `windows-x64.7z` (7-Zip)
- A release bundle contains every preset component (all 23 presets: extensions, LSPs, themes, node runtime, remote server for x86_64 trio). Extract and run.

linux:

```bash
tar -I zstd -xf zed-portable-v1.15.0-linux-x64.tar.zst
./run.sh
```

windows (7-Zip required):

```powershell
7z x zed-portable-v1.15.0-windows-x64.7z
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

On machines without a GPU, the software rendering warning can block first launch; set `ZED_ALLOW_EMULATED_GPU=1` and retry. Runtime details and the `--user-data-dir` mechanics are in "Using the bundle" below; `BUILD_INFO` inside the bundle records the build ledger (platform, Zed tag/commit, extensions commit, build date).

## Build from source (developers)

Requires [uv](https://docs.astral.sh/uv/) (it manages python 3.12, dependency sync, and command execution).

```bash
# 1. Sync dependencies (first run; driven by pyproject.toml + committed uv.lock)
uv sync

# 2. Pick components: link the presets you want into enabled/ (symlink = enabled)
cd config/enabled
ln -s ../available/core-zed.toml     # core: zed itself
ln -s ../available/core-node.toml    # core: node + debugpy
ln -s ../available/web.toml          # domain: web development
ln -s ../available/lang-rust.toml    # language: rust
ln -s ../available/themes.toml       # polish: themes
# Or write your own delta file (appends/overrides presets; a z prefix merges last):
#   vim zz-mine.toml
#   [extensions]
#   ids = ["zig", "erlang"]
#   [lsp.github]
#   gopls = true

# 3. Build (outputs to dist/)
cd ..
uv run zed-portable build
```

Notes:

- `config/available/` is the candidate library (git-tracked, ships with the project) and does nothing by default. `config/enabled/` (git-ignored) is the actual build input. Upgrade the project and you keep your setup: enabled/ holds only symlinks plus your delta files.
- An extension listed in several presets merges cleanly: arrays append, strings dedupe, installs are idempotent.
- Env vars override config (highest priority): `ZED_RELEASE_TAG`, `EXTENSIONS_REV` pin artifact versions without touching config files.

## Preset catalog

Full list: [docs/PRESETS.md](docs/PRESETS.md) (what the 23 presets install).

## Using the bundle (dist/)

```bash
# linux
./dist/run.sh                 # equivalent to: bin/zed --user-data-dir ./dist/data "$@"
# windows
.\dist\run.ps1
```

- **`--user-data-dir` mechanics**: Zed treats `data/` as its runtime data directory (`config_dir = data_dir/config`), so settings.json lands at `data/config/settings.json`. The pre-provisioned extensions, LSPs, and node all live under this directory; offline first launch hits the cache immediately.
- **Machines without a GPU**: the software rendering warning can block startup. Set `ZED_ALLOW_EMULATED_GPU=1` to bypass (the bundle does not ship GPU resources).
- **BUILD_INFO**: `dist/BUILD_INFO` records the build ledger: platform, zed tag/commit, extensions commit, build date, and the enabled config file list. It ships with every release artifact.

## Limitations and known behavior

- **npm LSP offline timeout**: Zed still runs `npm info` on every startup (child process, ~5s timeout, falls back to cache). Functionality works, but startup has a perceptible delay (DESIGN.md §6).
- **GitHub LSP offline timeout**: background version check fails (~10s timeout) then falls back to cache; the LSP starts normally.
- **wasm extensions with bundled LSPs** (gleam/deno/latex(texlab)/zig(zls)/erlang...): their LSPs download at runtime into `work/{id}/`; offline you get syntax highlighting only. Pre-provisioning approach: DESIGN.md §5.5 (best-effort, never blocks the build).
- Extension marketplace is unavailable offline (expected); pre-provisioned extensions load purely locally.
- Full offline behavior matrix and the list of "attempted requests": DESIGN.md §6.

## Directory layout

```
zed-portable/
├── pyproject.toml / uv.lock / .python-version   # uv project (python 3.12)
├── src/zed_portable/                      # the builder (cli/config/download/.../finalize)
├── config/
│   ├── available/                               # candidate preset library (inert by default)
│   └── enabled/                                 # effective config (ln -s presets + your deltas; git-ignored)
├── scripts/sort_by_mime.py                      # reorder files by mime for better zstd ratio
├── .github/workflows/build-bundle.yml           # CI build + release
├── build/                                       # intermediates (git-ignored)
└── dist/                                        # the bundle (git-ignored): bin/zed + data/ + run.sh/run.ps1 + BUILD_INFO
```

## Design

Pipeline stages P0-P6 and the full offline behavior matrix live in [docs/DESIGN.md](docs/DESIGN.md) (Chinese).
