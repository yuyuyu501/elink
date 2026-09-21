# Elink 0.4.4 Preview

Windows 独立远程桌面与游戏串流原型。Elink 自己负责采集、发现、会话、播放器和输入；不需要 Sunshine 或 Moonlight，不下载或启动它们。

## 运行

Windows 10/11 x64，当前测试环境为 Windows + Python 3.13。

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe main.py
```

本地便携构建入口：`dist/Elink/Elink.exe`，需保留整个文件夹。构建：`.venv/Scripts/python.exe -m scripts.build`。

日常开发按“本地修改 → 自动测试 → Git 同步 → Windows 打包 → GitHub Release”执行，发布后由用户安装新版并手动测试。更新版本号、写好发布说明并审阅暂存修改后运行 `.venv/Scripts/python.exe -m scripts.release_windows --message "修改说明" --notes docs/releases/v0.4.4.md`。各阶段失败即停止；打包与上传脚本也可单独重试。详见 [开发与打包流程](docs/development-workflow.md)。

自动化测试：`.venv/Scripts/python.exe -m pytest -q`。便携包自测可运行 `Elink.exe --self-test <独立测试目录>`，结果写入该目录；实际桌面采集自测为 `--self-test-desktop`，两者均不向系统注入键鼠。

双机测试入口：`Elink.exe --peer-test <config.json>`，使用独立身份和输入记录器。2026-09-18 已通过 Tailscale 在两台 Windows 电脑上测试；实际桌面仍存在卡顿，结果与复现步骤见 [双机测试报告](docs/two-machine-test-20260918.md)。

## 连接两台 Windows 电脑

1. 两端启动 Elink。启动后默认可被控，同一时间只允许一台控制端接入，默认 HTTPS TCP 49200。Windows 防火墙需允许 Elink 入站；媒体使用 ICE 动态 UDP 端口。
2. 如果两端没有可达地址，先自行安装、登录外部 Tailscale，并让两端在允许互访的同一 tailnet 内。Elink 不修改 Tailscale 账户或系统网络设置。
3. 控制端“设备”页会自动发现同局域网和同 Tailscale 下正在运行 Elink 的机器。相同机器的多个地址会合并为一项，优先使用局域网地址；点击设备即可开始连接，也可使用“通过地址连接”。本机状态和断开当前控制端按钮也位于设备页。
4. “设置”页保存默认串流偏好；串流窗口上方的“控制中心”可调整画质、鼠标、手柄、本机静音、统计和全屏。画质与声音传输设置通过“应用并重新连接”生效，鼠标、手柄和静音立即生效。点击画面捕获输入；F8 打开控制中心并释放输入，Ctrl+Alt+Shift+Z 释放输入，F10 显示 / 隐藏统计，F11 切换全屏，Ctrl+Alt+Shift+Q 断开。Esc 正常发送给游戏。游戏可从远端桌面启动。
5. 在主机点击“断开当前控制端”可结束会话，随后自动恢复等待连接。退出程序会释放输入并停止接入。没有开机启动或后台系统服务。

旧 0.2 版本的 GameStream 配对不可复用；0.3 使用独立 `desktop-v3.json` 设置，旧配置与用户数据保留。

首页不再显示常驻日志或串流参数表单，连接失败以可选择复制的提示显示。详细记录在“网络诊断 → 查看诊断记录”中，默认收起。监听端口与本机手柄后端移入“设置 → 高级设置”。

### 实时状态、鼠标显示与应用更新

串流画面左上角显示紧凑纵排统计：会话时长、实际路径、FPS、接收 Kbps、RTT、帧间隔、视频丢包率 / 抖动、解码耗时、目标 Mbps 和解码器。F10 可隐藏。未知数据显示 `--`；Tailscale 直连或中继通过实际探测区分。详细测量口径见 [串流统计说明](docs/stream-statistics.md)。

主机现在将 Windows 系统光标（位置、形状、热点和可见状态）合成到串流画面。控制端捕获输入后隐藏本地指针，释放输入后恢复；游戏主动隐藏系统光标时不会强制显示箭头。**此修复需要更新被控端**，建议两端都更新。桌面操作可取消“游戏相对鼠标”，游戏使用相对鼠标模式。光标随视频传输，仍受视频延迟影响。

“关于”页显示版本并提供“检测更新”，可选择包含预览版本。发现新版后先询问；同意后下载完整 Windows x64 便携 ZIP、校验 SHA-256、解压检查，随后断开串流、退出并自动替换和重启。下载可以取消，启动失败会尝试恢复旧版；旧目录保留在应用旁的 `.Elink-update-*` 内，确认新版正常后可删除。不会静默更新或安装驱动。

自动替换仅支持解压后的便携版，目录及上级目录需可写，需预留 3 GiB 加下载包大小的空间。用户配置和配对数据仍在 `%LOCALAPPDATA%\Elink`，或外置的 `ELINK_DATA_DIR`；不支持将数据放在应用文件夹内进行自动更新。源码运行时可检测版本并前往 Releases 手动下载。下载依赖能访问 GitHub；网络失败或 API 限流时可重试或手动更新。

**0.4.0 EXE 没有更新入口，需要先手动安装一次 0.4.1 或更高版本。** 后续可通过“关于 → 检测更新”升级。更新说明、校验与回退边界见 [应用更新说明](docs/application-updates.md)。

## 当前能力

- DXGI 桌面采集，H.264 硬件编码优先 NVENC / AMF / QSV，失败时使用 libx264。
- Elink 窗口播放，D3D11VA 解码优先、软件回退，最新画面队列有界。
- aiortc WebRTC：ICE、DTLS/SRTP、RTP/RTCP、NACK/PLI、拥塞反馈；只协商 H.264 视频和 Opus 音频。
- WASAPI 系统声音采集、立体声播放；键盘按住/释放、相对或绝对鼠标、滚轮。
- 控制端 XInput 最多四个手柄；主机可选择 ViGEmBus 兼容模式、ElinkPad 实验驱动或禁用手柄。兼容模式保留现有四手柄及震动；ElinkPad 当前限单手柄，选择后不会暗中回退至 ViGEm。
- 同网自动发现与直接连接、单控制端占用检查、会话内证书固定；失焦/断线/超时释放输入。主机证书私钥使用 DPAPI 保存。
- 外部 Tailscale 状态、设备列表、直连或中继路径探测。

## 尚未完成

这是可验证的独立核心预览版，尚未达到成熟游戏串流产品的性能与兼容性。分辨率上限 1080p、目标帧率上限 120 FPS；不是性能承诺。Qt 图像绘制和 CPU/GPU 拷贝尚未改成零拷贝渲染。通道 RTT 不代表操作到显示的总延迟。

没有自有信令服务器、公网 STUN/TURN 或内置 Tailscale。没有可达路径且 Tailscale 不可用时，无法连接。跨 NAT 不作成功保证；中继可能增加延迟。

目前只串流主显示器当前桌面；没有 HDR、HEVC/AV1、多显示器选择、专用游戏启动列表、剪贴板/文件传输、UAC/锁屏控制、服务模式、独立窗口级采集。F8 / F10 / F11 被播放器保留。Linux 和 Android 尚未实现，不计划 macOS/iOS。

NVENC/D3D11VA 可在本机验证；AMF/QSV、实际物理手柄游戏和跨网表现需要对应设备与第二台电脑验收。

## 自主手柄驱动进度

`native/elinkpad` 新增 Elink 自己维护的 UMDF2/XUSB 实验驱动，不需要 Sunshine、Moonlight 或 ViGEm 才能编译。当前实现单手柄、版本化 IOCTL、句柄控制权、震动反馈和 500 ms 失联归零。应用用独立工作线程提交状态，避免设备访问阻塞串流。

驱动已编译，并完成原生协议测试、INF 校验和未签名目录文件生成；尚未安装到工作机，也未完成 Windows XInput 或真实游戏验证。**当前默认仍是已验证过的 ViGEm 兼容模式，不应理解为已彻底替代 ViGEm。** 实验选项需要在独立测试系统签名、加载驱动，并运行临时设备助手；未就绪会明确报错，键鼠和串流仍可使用。

构建：`.venv/Scripts/python.exe scripts/build_elinkpad.py --fetch-wdk`。工具只在项目目录取得 WDK 和编译，不安装驱动、不导入证书、不修改系统启动设置。详见 [驱动开发说明](native/elinkpad/README.md) 和 [更新验收记录](docs/elinkpad-upgrade.md)。

实现与验证记录见 [docs/implementation.md](docs/implementation.md)。第三方库和再分发要求见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
