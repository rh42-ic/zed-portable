# CI 与发布

`.github/workflows/build-bundle.yml`（仅 tag 触发）：

1. **push tag `bundle-*`** → 构建 linux-x64 + windows-x64 双平台 → release job 打包并 `gh release create`：
   - linux 产物 `tar.zst`：打包前 `scripts/sort_by_mime.py` 按 mime 排序文件列表（zstd `--long=27` 长距离匹配率更高，级别 `-9`）
   - windows 产物 `7z -mx=9`
   - 均含 BUILD_INFO；release note 标注对应 Zed 版本（`bundle-v1.15.0` → Zed v1.15.0）并附原版发布链接
2. **workflow_dispatch**（手动）→ 只构建双平台产物并上传 artifact，不发布
3. CI 每次重建 `config/enabled/`（git 忽略）：全量链接全部 23 个 preset 后执行与本地相同的 `uv sync` → `zed-portable build`；全程 uv，禁止 pip

## 版本约定

- release tag 命名 **`bundle-<zed-tag>`**（如 `bundle-v1.15.0`），与 Zed 上游 tag 一一对应
- 固定产物版本：`ZED_RELEASE_TAG`（zed tag，缺省取 channel 最新 stable）/ `EXTENSIONS_REV`（extensions 仓库 commit，缺省 HEAD），优先级高于配置文件；实际值记录到 BUILD_INFO
- 同一配置下，CI 与本机可分别用 env 显式控制版本
