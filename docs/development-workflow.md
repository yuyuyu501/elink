# 开发、打包与发布流程

2026-10-16 起，用户授权在明确修复并验证一个具体问题后，完成：**本地修改 → 自动测试 → Git 同步 → Windows 打包自测 → Preview 发布**，无需再次询问。按全局规则区分问题讨论与代码任务；普通问答不自动触发开发流程。项目开关见 `AGENTS.md`：`local_change: 1`、`test: 1`、`git_sync: 1`、`server_update: 0`、`windows_package: 1`、`github_release: 1`。

调查、未完成的实验和仅文档更新不自动发布。发布前记录具体问题的修复证据，并披露尚未解决的限制；单元测试通过不能代替性能问题的实际验证。用户要求仅本地修改时按该要求执行。开发用原生编译、源码运行和 GPU 测量不属于应用打包；不会更新服务器或安装驱动。

故障尚未定位且现有报告只有异常类型时，先补齐文件日志及报告中的异常消息、堆栈和失败阶段，并验证真实失败能留下这些信息。用户要求的诊断预览版须明确说明根因仍待确定，不能将提示样式等外围修复作为底层故障已经解决的证据。

## 默认入口

先检查工作区、分支、远端和相关 Git 历史，保留无关修改。完成修改与相关检查后审阅差异，仅暂存本次文件：

```powershell
git status --short
git add <本次修改的具体文件>
.venv/Scripts/python.exe -m scripts.check_and_sync --message "说明本次修改"
```

同步后核对本地 HEAD 与远端分支一致。测试或推送失败时保留工作和确切提交并报告，不强推或自动合并远端分叉。性能报告和临时产物放在忽略的 `.artifacts`，不提交机器身份、配置或凭据。性能测试方法见 [串流性能测量与 GPU 原型](streaming-performance.md)。

## 完成明确修复后的打包发布

每次发布使用未被占用的新版本，默认递增补丁号，同时更新 `elink/__init__.py` 和 `pyproject.toml`。在 `docs/releases/v<版本>.md` 写好此次发布说明，审阅并暂存本次文件，然后运行：

```powershell
git status --short
git add <本次修改的具体文件>
.venv/Scripts/python.exe -m scripts.release_windows --message "说明本次修改" --notes docs/releases/v0.4.2.md
```

请将示例说明路径换成本次版本。该入口依次调用三个独立阶段，任一失败立即停止：

1. `check_and_sync`：完整 pytest，确认测试期间源码及暂存区未变，再检查上游。只提交已审阅/暂存文件并 push；不强推、不丢弃修改、不自动合并分叉。不自行 `git add --all`，存在未暂存或未跟踪源码时停止。
2. `package_windows`：重跑测试，构建 PyInstaller 便携目录，运行真实 EXE 音视频/退出自测，再用隔离副本验证目录替换、重启与配置保留，检查源码未变，最后生成 ZIP 与校验报告。
3. `publish_release`：确认包与干净工作区、远端 main 提交一致，且对应 push 的 GitHub Windows CI 成功。先创建预览版草稿，上传并核对所有资产大小及 SHA-256，再公开发布。已发布版本不覆盖；同一提交的未完成草稿可继续上传缺失的资产。

GitHub Actions 补跑 Windows 单元测试并编译 D3D11 视频桥接库，不直接打包。构建仍在本地 Windows 桌面环境进行，需要项目 `.venv`、`pip install -e ".[dev]"`、Visual Studio C++ Build Tools、Windows SDK、CMake 和可用音频输出设备。RTX 实机自测还需支持的显卡和已开启的 NVIDIA 视频超分。发布使用 Git Credential Manager 内已有的 GitHub HTTPS 凭据，只在内存读取，不写入仓库或产物。

## 发布阶段失败后独立重试

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

ZIP 是便携目录应用，不能只发其中的 EXE。`_internal` 包含媒体和界面运行库；不需要用户安装 Python。不会每次启动解压全部库，也保留动态库可替换性。另提供包含相同完整程序的离线 Setup 安装包，流程见下节。

真实 EXE 更新自测使用同版本副本，检查替换机制，不代表未来版本的数据迁移正确。发布仍标记 Preview、未签名和用户手动验收待完成。用户安装后重点检查双机连接、光标、键鼠、声音、手柄和游戏表现；发现问题再修复并发布新版本，不改写已发布资产。

ElinkPad 实验驱动不会随应用更新安装。第三方组件要求见 `THIRD_PARTY_NOTICES.md`；双机性能已知问题见 `two-machine-test-20260918.md`。

## 完整离线安装包

安装器使用 Inno Setup 6.7+，编译器从 [官方网站](https://jrsoftware.org/isdl.php) 获取并核对 Authenticode 签名。中文消息来自 [Inno Setup 官方仓库](https://github.com/jrsoftware/issrc/blob/main/Files/Languages/ChineseSimplified.isl)，以原始文件保存在 `packaging/windows/ChineseSimplified.isl`。

用户已授权为 v0.4.37 追加完整安装包。该操作封装已经发布且校验一致的 ZIP，不重新构建应用，不修改版本标签、原 ZIP、校验文件、验证报告或发布说明。后续新版本仍先走正常发布流程；需要 Setup 时使用下列独立入口：

```powershell
.venv/Scripts/python.exe -m scripts.package_installer --package dist/windows-v0.4.37/releases/v0.4.37/<原ZIP的UTC时间> --compiler <Inno安装目录>/ISCC.exe
.venv/Scripts/python.exe -m scripts.publish_installer --package dist/installers/v0.4.37/<安装包UTC时间>
```

脚本要求干净的已提交源码，读取原 ZIP 和两份原始校验报告，并核对它们与 GitHub 已发布资产的大小、SHA-256 和版本标签提交完全一致。安装器将完整程序放入 `%LOCALAPPDATA%\Programs\Elink\app`，卸载器位于其上一级。安装不需要管理员权限，不下载运行库、不安装驱动、不自动打开开机启动；桌面快捷方式默认不勾选。用户配置仍为 `%LOCALAPPDATA%\Elink`，卸载后保留。

构建后在含中文、空格和方括号的隔离目录执行实际安装、程序音视频自测、现有 ZIP 更新助手替换及重启、重复安装和卸载，核对程序文件逐字节一致、开始菜单路径、卸载注册信息、更新后卸载器保留及配置保留。测试只创建自己的开始菜单分组；检测到已有正式安装时停止，改用隔离 Windows 账户测试，不能覆盖用户安装。失败诊断保留在 `.artifacts/installer-check-*`。用户原有便携进程不参与测试。

产物为 `Elink-<版本>-windows-x64-Setup.exe`、独立的 `Setup-SHA256SUMS.txt` 和 `setup-verification.json`，后者分别记录应用提交和安装工具提交。上传前要求安装工具提交已同步到 main，且 Windows CI 成功；只追加新资产，同名资产只有字节完全一致才允许继续，绝不删除或替换已发布文件。上传后再次检查标签、已有资产和发布元数据未变。

安装版继续使用现有的 ZIP 自动更新，只替换 `app` 子目录，因此快捷方式和卸载器能够保留。Windows“已安装的应用”中的版本记录由 Setup 写入，应用内 ZIP 更新不会刷新此记录，可在 Elink 关于页面查看实际运行版本。安装器未签名，仍属于 Preview，人工双机验收与自动测试分开。

仅修复安装器时，不重新构建已发布应用。构建及上传命令同时增加 `--installer-revision 2`（再次修改则递增），输出 `Setup-r2.exe`、`Setup-r2-SHA256SUMS.txt` 和 `setup-r2-verification.json`，追加到同版本发布并保留全部旧资产。安装工具提交和版本文件属性会记录修订号。

目录选择页和准备安装页始终显示安装路径，包括已有安装记录的情况。路径遵循 Windows 通常不区分大小写的规则，测试会使用中文、空格、方括号目录，并通过改变路径大小写后重复安装，验证仍然使用同一个目录；不声称支持启用了大小写敏感属性的特殊 NTFS 目录。
