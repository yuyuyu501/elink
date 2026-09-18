# 开发与打包流程

日常流程固定为：**本地修改 → 自动测试 → Git 同步**。
只有用户明确要求打包或发布时，才执行独立打包脚本。普通提交、测试通过和 push 均不会触发构建 EXE、创建 tag 或 GitHub Release。

## 日常更新

先检查并暂存本次修改，再运行：

```powershell
git status --short
git add <本次修改的文件或目录>
.venv/Scripts/python.exe -m scripts.check_and_sync --message "说明本次修改"
```

脚本运行完整 pytest 测试集，确认测试期间源码和暂存区没有变化，再拉取上游状态；只有远端可快进时才提交并 push。测试失败、工作区未审阅、远端有新提交或认证失败时返回非零退出码，不强推、不自动合并、不打包。

脚本只提交已暂存的修改，不自行执行 `git add --all`。非忽略的未暂存/未跟踪文件会导致提前停止，防止测试代码与提交代码不一致。存在其他任务的修改时，由开发者分别审阅和提交，不得丢弃它们来通过检查。push 失败后已创建的本地提交会保留；解决认证/网络问题后重跑脚本即可测试并重试同步。

GitHub Actions 在 main 推送或 PR 时补跑 Windows 测试；它不打包、不上传二进制、不创建 Release。远端 CI 不能替代本地测试，也不能替代双机/游戏验收。

## 按需 Windows 打包

仅在明确要求打包时运行：

```powershell
.venv/Scripts/python.exe -m scripts.package_windows
```

需要 Windows x64、项目 `.venv` 和 `pip install -e ".[dev]"`。脚本执行测试、现有 PyInstaller 构建、打包后的 EXE 自测，并检查声音/画面帧和退出清理。自测使用合成画面与静音音频，需要可用的 Windows 桌面和音频输出设备；不会安装驱动或注入系统输入。

成功后的产物：

- `dist/Elink/Elink.exe`：直接运行的 Windows 程序。
- `dist/releases/v<版本>/<UTC时间>/Elink-<版本>-windows-x64.zip`：完整便携包。
- 同目录 `SHA256SUMS.txt` 与 `verification.json`：校验值、源码文件散列、Git 提交/脏工作区状态、自测结果。

EXE 是便携目录程序，运行时需要旁边的 `_internal` 和 DLL；分发应发送完整 ZIP，不能只复制 EXE。这样保留可替换的动态库，也避免单文件程序每次启动解压全部媒体组件。后续需要单文件自解压版或安装器时单独增加。

脚本自动读取 `elink.__version__` 和 `pyproject.toml` 并要求两者一致；不会自动升级版本。不跳过测试，不覆盖已有成功发布目录；失败会停止，`dist/Elink` 可能留下本次未验收构建，以非零退出码和缺少新的验证目录为准。脚本不提交、不 push、不打 tag、不创建或发布 GitHub Release。

当前仍是未签名预览版。独立 ElinkPad 驱动不包含在应用安装流程内。公开二进制发行前应按 `THIRD_PARTY_NOTICES.md` 准备对应依赖的源码和许可证材料。双机性能问题见 `two-machine-test-20260918.md`。
