# 开发、打包与发布流程

2026-09-21 起，用户授权的默认流程为：**本地修改 → 自动测试 → Git 同步 → Windows 打包 → GitHub Release → 用户安装后手动功能测试**。每次完成应用更新都按此流程推进，无需再次询问是否打包发布。不会安装驱动。

## 默认入口

先检查工作区和远端，保留无关修改。每次发布使用未被占用的新版本，默认递增补丁号，同时更新 `elink/__init__.py` 和 `pyproject.toml`。在 `docs/releases/v<版本>.md` 写好此次发布说明，审阅并暂存本次文件，然后运行：

```powershell
git status --short
git add <本次修改的具体文件>
.venv/Scripts/python.exe -m scripts.release_windows --message "说明本次修改" --notes docs/releases/v0.4.2.md
```

请将示例说明路径换成本次版本。该入口依次调用三个独立阶段，任一失败立即停止：

1. `check_and_sync`：完整 pytest，确认测试期间源码及暂存区未变，再检查上游。只提交已审阅/暂存文件并 push；不强推、不丢弃修改、不自动合并分叉。不自行 `git add --all`，存在未暂存或未跟踪源码时停止。
2. `package_windows`：重跑测试，构建 PyInstaller 便携目录，运行真实 EXE 音视频/退出自测，再用隔离副本验证目录替换、重启与配置保留，检查源码未变，最后生成 ZIP 与校验报告。
3. `publish_release`：确认包与干净工作区、远端 main 提交一致，且对应 push 的 GitHub Windows CI 成功。先创建预览版草稿，上传并核对所有资产大小及 SHA-256，再公开发布。已发布版本不覆盖；同一提交的未完成草稿可继续上传缺失的资产。

GitHub Actions 补跑 Windows 单元测试，不直接打包。构建仍在本地 Windows 桌面环境进行，需要项目 `.venv`、`pip install -e ".[dev]"` 和可用音频输出设备。发布使用 Git Credential Manager 内已有的 GitHub HTTPS 凭据，只在内存读取，不写入仓库或产物。

## 失败后独立重试

```powershell
# 测试并同步源码，不执行后两个阶段
.venv/Scripts/python.exe -m scripts.check_and_sync --message "修改说明"

# 构建/自测/生成便携 ZIP，不做 Git 或网络发布
.venv/Scripts/python.exe -m scripts.package_windows

# 上传已有验证包，不重新构建；路径替换成本次成功产物
.venv/Scripts/python.exe -m scripts.publish_release --package dist/releases/v0.4.1/<UTC时间> --notes docs/releases/v0.4.1.md
```

如果 CI 仍在运行，保留包并等待 CI 完成后重试上传。上传中断保留草稿；已有资产若校验不一致，停止并报告，不自动删除或覆盖。push 失败保留本地提交；远端分叉需要先审阅解决，再重新测试。源码发生变更后必须同步并重新打包，不能发布旧工作区产物。

## 产物与验收

- `dist/Elink/Elink.exe`：Windows 可运行程序，必须保留整个文件夹。
- `dist/releases/v<版本>/<UTC时间>/Elink-<版本>-windows-x64.zip`：完整便携包。
- 同目录 `SHA256SUMS.txt` 和 `verification.json`：ZIP 校验、源码散列、提交、EXE 和更新自测、待用户验收状态。

这是便携目录应用，不能只发 EXE。`_internal` 包含媒体和界面运行库；不需要用户安装 Python。不会每次启动解压全部库，也保留动态库可替换性。安装器或单文件自解压版不属于当前发布格式。

真实 EXE 更新自测使用同版本副本，检查替换机制，不代表未来版本的数据迁移正确。发布仍标记 Preview、未签名和用户手动验收待完成。用户安装后重点检查双机连接、光标、键鼠、声音、手柄和游戏表现；发现问题再修复并发布新版本，不改写已发布资产。

ElinkPad 实验驱动不会随应用更新安装。第三方组件要求见 `THIRD_PARTY_NOTICES.md`；双机性能已知问题见 `two-machine-test-20260918.md`。
