# Elink 项目规则

## 工作流

```yaml
workflow:
  local_change: 1
  test: 1
  git_sync: 0
  server_update: 0
```

普通修改在本地完成并验证。用户要求 Git 上传、打包或发布时，只启用对应步骤；本地预览不自动触发正式发布。

## 项目结构

沿用现有目录，不另建源码副本或平行的打包目录：

| 位置 | 用途 |
| --- | --- |
| `main.py` | 程序入口 |
| `backend/` | 设备发现、连接、媒体及远控后端 |
| `frontend/desktop/` | PySide6 桌面界面 |
| `frontend/mobile/`、`frontend/shared/` | 移动端及共享前端资源 |
| `assets/`、`licenses/` | 素材及第三方许可 |
| `tests/` | 回归测试 |
| `scripts/` | 检查、构建及打包脚本 |
| `packaging/windows/`、`packaging/linux/` | 安装器和平台打包配置 |
| `.github/workflows/` | 自动测试和正式发布流程 |
| `docs/` | 本地计划及项目文档 |
| `releases/v<version>.md` | 随源码提交的发布说明 |
| `out/` | 临时日志、诊断及预览文件 |
| `dist/Elink/` | 当前本地可运行程序 |
| `dist/<version>/` | 对应版本的正式发布文件 |

源码、打包脚本和安装器配置进入 Git；`docs/`、`out/`、`dist/`、虚拟环境及构建中间文件按现有 `.gitignore` 留在本地。Python 的 `*.egg-info` 属于生成的元数据，不作为项目源码或交付文件保留。

## 代码与验证

- 修改现有实现，保持最小且完整的改动，不添加无关代码。
- 修改前检查当前分支和工作区，保留已有改动；Git 同步使用已确认的源码仓库和分支，不另建嵌套仓库。
- 回归检查使用 `python -m scripts.check`；界面自检使用 `python main.py --self-test <报告路径>`，报告及截图放在 `out/`。
- 本地需要运行 Python 时使用独立虚拟环境；不为打包修改系统或 Conda 的全局依赖。

## 正式打包与发布

正式 Windows 发布统一使用 `.github/workflows/windows-release.yml`，不临时改为本机下载、安装编译器并手工制作正式安装包。

1. 保持 `pyproject.toml` 的版本、`frontend/desktop/__init__.py` 的版本和 `releases/v<version>.md` 一致。
2. 按用户授权提交并推送已验证的代码；正式发布推送对应的 `v<version>` 标签，确认标签指向本次发布提交。
3. 由 Actions 的 Windows 环境安装 Python、项目依赖、ffmpeg 和 Inno Setup，执行 `python -m scripts.package_windows --dist-dir dist`。沿用该脚本完成回归测试、PyInstaller 构建、打包后 EXE 自检、ZIP 和安装包生成。
4. 检查对应提交和标签的工作流结果、自检报告及发布资产。缺少安装包或自检失败时，不报告正式打包完成。
5. 由工作流上传便携 ZIP、安装包、`SHA256SUMS.txt` 和 `verification.json` 到对应 GitHub Release。仅构建验收而未授权发布时，使用手动触发工作流及其 artifact。
6. 将同一次 Actions 构建的发布文件下载回本地 `dist/<version>/`，核对文件完整性及 SHA-256；不得混用本地重打包文件和云端发布文件。

本地正式归档结构：

```text
dist/
  Elink/
  <version>/
    Elink-<version>-windows-x64.zip
    Elink-<version>-windows-x64-setup.exe
    SHA256SUMS.txt
    verification.json
    package-summary.json
  package-summary.json
```

`package-summary.json` 记录该次构建的版本、来源提交或工作流、产物位置和验证结果；从对应构建日志取得或依据已核对的同批产物整理，不复用旧版本摘要。根目录摘要指向最新完成的版本。

## 本地程序与目录管理

- 用户要求本地查看效果时更新 `dist/Elink/`；使用正式便携 ZIP 时完整解压程序目录，不只替换 EXE。
- 更新后运行打包程序的 `--self-test` 并检查正常启动，确认 Qt、资源和媒体链路可用后再报告完成。
- 历史版本保存在各自的 `dist/<version>/`，不覆盖或删除其他版本。
- 不创建 `dist-*`、`stage*`、`diag*` 等平行交付目录；临时工作使用 `out/`，PyInstaller 的中间文件仅使用既有 `dist/build/` 和 `dist/Elink.spec`。
- 清理仅限本次生成且已确认不用的中间文件；删除前确认准确路径，不删除程序依赖、源码或历史发布资产。
- 安装包继续使用现有 `packaging/windows/elink.iss` 的 AppId 和安装目录约定，保留用户配置。

正式发布方式以本规则为准；`RELEASING.md` 中的本地打包方式只用于用户明确要求的本地构建。
