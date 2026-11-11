# 发布流程

目标仓库沿用旧版 Elink 的地址：`https://github.com/yuyuyu501/elink`。
如果实际仓库不同，请同步修改 `packaging/windows/elink.iss` 中的
`MyRepoURL`、README 链接和本文档。

## 1. 版本号

三处必须一致：

- `pyproject.toml` 的 `[project].version`
- `frontend/desktop/__init__.py` 的 `__version__`
- 发布说明文件 `releases/v<版本>.md`（`docs/` 默认不进 Git）

安装包沿用旧版的 AppId 和默认目录，安装新版会覆盖旧程序文件，保留
`%LOCALAPPDATA%\Elink` 下的用户配置。

## 2. 本地完整打包

需要 Windows x64、Python 3.11–3.13、PyInstaller；安装包需要 Inno Setup 6。
构建时通过 `ELINK_FFMPEG_DIR` 指向实际 ffmpeg 发行版的 `bin` 目录
（含 libx264、SRT，且上级目录保留许可证）；不可使用 Chocolatey 启动器。
可以复用本机已有的 ISCC：

```powershell
$env:ELINK_ISCC = 'E:\elink\.artifacts\toolchains\inno-6.7.3\ISCC.exe'
python -m scripts.package_windows --dist-dir dist
```

产物按版本归档在一个 `dist/` 下：

```text
dist/
├── Elink/                                   # 当前 onedir 应用
├── <version>/                               # 每个版本一个目录
│   ├── Elink-<version>-windows-x64.zip      # 便携包
│   ├── Elink-<version>-windows-x64-setup.exe# Inno Setup 安装包
│   ├── SHA256SUMS.txt
│   ├── verification.json
│   └── package-summary.json
└── package-summary.json                     # 最近一次构建摘要
```

多次构建不同版本时只新增 `dist/<version>/`，根目录不再出现多个 `dist-*`。

`scripts/package_windows.py` 会先跑回归测试，再用打包后的 exe 跑
`--self-test`（界面、后端 import、编码/解封装/解码闭环）。自检失败不会产出发布资产。
自检清除系统 ffmpeg 的 PATH，并使用临时配置目录，要求内置 ffmpeg 支持
libx264 和 SRT、EXE 正常退出且报告通过。

如果 `ELINK_ISCC` 未设置，脚本会查找 `Program Files` 下的 Inno Setup 6，
或 PATH 中的 `ISCC`；找不到时构建失败。仅需便携 ZIP 时显式传入 `--no-installer`。

## 3. GitHub Actions（推荐）

- `.github/workflows/test.yml`：push / PR 时在 Windows runner 上跑
  `scripts.check` + UI 自检。
- `.github/workflows/windows-release.yml`：推送 `v*` 标签时自动
  安装 ffmpeg / Inno Setup、打包、自检、上传 artifact，并用
  `softprops/action-gh-release` 发布 Release。

发布步骤：

```powershell
git tag v0.6.0
git push origin v0.6.0
```

工作流会校验 tag 与 `__version__` 一致，读取
`releases/v0.6.0.md` 作为 Release 正文，上传：

- `dist/<version>/Elink-<version>-windows-x64.zip`
- `dist/<version>/Elink-<version>-windows-x64-setup.exe`
- `dist/<version>/SHA256SUMS.txt`
- `dist/<version>/verification.json`

Actions 使用仓库自带的 `GITHUB_TOKEN`（`permissions: contents: write`），
不需要把个人 token 写进代码或本地环境。

## 4. 手动上传

如果暂时不用 Actions，可在 GitHub Release 页面手动上传上面的四个文件；
发布说明直接使用 `releases/v<version>.md`。

## 5. 替换旧版

- 安装包 AppId 与旧版相同（`{A4735807-684B-4757-9EE5-88AFA2E6E791}`），
  默认目录相同，安装新版会替换旧版程序。
- 旧版用户配置不会被卸载删除。
- 旧版的启动项 `HKCU\...\Run\Elink` 会在新版开机自启开关下继续更新；
  卸载时如果命令指向当前安装目录会删除该值。

## 6. 签名与安全

当前安装包和 exe **未代码签名**，SmartScreen 可能提示；这是发布说明中的已知限制。
若后续配置了代码签名证书，请在 `scripts/package_windows.py` 增加签名步骤，
并在 `verification.json` 中把 `signed` 改为 `true`。
