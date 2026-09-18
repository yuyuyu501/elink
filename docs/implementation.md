# Windows 独立核心实现记录

日期：2026-09-14。版本：0.3.0 Preview。替代 0.2 的 Sunshine/Moonlight 进程集成方案。

本文保留 0.3 独立串流核心的实现与历史验证；0.4 自主手柄驱动实验、后端切换与最新验证见 [ElinkPad 升级记录](elinkpad-upgrade.md)。

## 结构

```text
Elink Qt 界面 / 本机批准 / Elink 播放器
  -> 独立 asyncio 线程
  -> Elink HTTPS 配对 + SDP 信令 (TCP 49200)
  -> aiortc ICE + DTLS/SRTP + SCTP
     视频: DXcam DXGI -> PyAV NVENC/AMF/QSV/x264 -> H.264 RTP
           -> D3D11VA/软件解码 -> 有界最新帧 -> Qt 图像绘制
     音频: WASAPI loopback -> PCM -> Opus -> 本机音频输出
     输入: 有序可靠键鼠控制 + 无序不重传鼠标/手柄状态
           -> SendInput / ViGEmClient -> 震动反馈 -> XInput
  -> 外部 Tailscale CLI (可选，网络层)
```

Elink 不再导入 GameStream 适配模块，不使用 Sunshine/Moonlight 可执行文件、配对状态或配置。通用库负责标准编解码和 WebRTC，应用拥有自己的协议、状态机与 UI。

## 信任与生命周期

临时邀请含 128 位随机量，180 秒有效。首次无凭据 TLS 握手只获取证书指纹；邀请 HMAC 绑定该指纹、随机 nonce、设备名和请求用途，固定证书后提交。主机本地批准后发放随机 token，服务端只保存其哈希，客户端用当前 Windows 用户 DPAPI 保存。所有后续 HTTPS 请求固定证书，不跟随重定向。

传输层使用 WebRTC 的 DTLS/SRTP，不手写加密协议。ICE 服务器列表为空，只收集本地接口候选，包括外部 Tailscale 接口。没有公共 STUN/TURN 或自有协调服务器。主机一次允许一个会话；认证、会话协商、撤销与停止由同一事件循环协调。

输入按会话激活 epoch 隔离；鼠标/手柄快照序号拒绝旧包。失焦立即发送释放，2 秒心跳超时释放按键，长时间无心跳结束会话。可靠控制发送积压时关闭连接，避免遗漏 key-up。撤销授权立即停止媒体并释放输入，并发送显式会话结束通知；通知丢失时控制端以 5 秒无心跳响应断开。

## 性能边界

发送端不排队捕获帧；音频采集保留至多 3 帧。解码输入最多 4 帧，视频溢出丢弃旧队列并请求 IDR；解码后视频至多 1 帧，GUI 至多 1 帧；音频输出队列至多 5 帧。aiortc 版本固定为 1.14.0，编解码工厂及接收队列适配依赖其内部接口，升级必须重测。

视频协商 H.264 level 5.2，当前设置限制 1080p / 120 FPS / 80 Mbps。编码码率响应 RTCP REMB 上限。DXGI 数据经过 CPU 色彩转换与 GPU 编解码拷贝，Qt 仍绘制 RGB 图像，未实现全链路零拷贝。UI 显示的通道 RTT、显示 FPS、解码器来自实际状态；没有端到端延迟承诺。

## 验证

`python -m pytest -q` 包含真实 HTTPS + WebRTC 回环：本地批准、证书固定与错误证书拒绝、视频像素变化、音频帧、可靠按键释放、手柄状态消息、撤销立即断开；输入使用记录后端，不操纵测试机桌面。另测非法设置、输入重放、失焦、心跳释放及 Qt 开启/停止/退出。

`python -m scripts.smoke_ui` 保存三个真实 Qt 页面在 1120×800、900×680 下的布局图。Windows Qt 后端实图已检查，文本和控件没有重叠。

本机 Windows 11 / RTX 4070 SUPER 实测：

- 19 项自动化测试通过，包括真实 TLS/WebRTC 回环、身份校验、输入释放与撤销、断开调用被取消后的清理、响应分块读取和有界接收队列。
- `python -m scripts.smoke_core`：真实 1920×1080、60 FPS 目标、20 Mbps 上限，8 秒短测累计收到 447 个视频帧、394 个音频帧（包含启动时间）；稳定阶段每秒约 60 帧。编码 NVENC，解码 D3D11VA，解码错误计数 0。主机撤销与两端关闭正常完成。这不是双机游戏性能基准。
- Windows UDP 关闭原先会等待积压的数据报；关闭 ICE 时改为丢弃待发数据报，且会话清理由独立任务完成，避免调用方取消导致清理半途停止。适配固定 aioice 0.10.2。
- 通用 ViGEmClient 在主机已有 ViGEmBus 上创建虚拟 Xbox 360 手柄；XInput 读回 buttons=4096、LX=1234，震动值经回调返回，测试结束后移除虚拟目标。没有安装驱动；未验证真实游戏兼容性。
- 本机音频输出设备已成功打开并播放静音 PCM 测试帧。WASAPI 实际采集会出现 discontinuity 警告，长时间音画同步及不同声卡仍需验证。
- `Elink.exe --self-test <独立测试目录>`：便携 EXE 自建回环配对、合成变化画面和音频、自己渲染播放器截图、撤销和退出。`--self-test-desktop` 改用真实桌面与系统声音采集，禁止系统输入注入，关闭声音回放以免同机反馈。两项均已通过；测试不使用正常用户配置目录。
- 自测输出在指定目录的 `result.json`、`player.png` 和三个页面截图。实际本机产物在 `.artifacts/packaged-selftest` 和 `.artifacts/packaged-desktop-selftest`，不随软件分发。

旧 Sunshine/Moonlight 下载器、适配层和启动脚本已移除。0.3 便携目录只包含 Elink 与通用库，构建记录位于 `build-info.json`。

仍需第二台 Windows 电脑验证：跨网/Tailscale直连及DERP路径、实际游戏操作、物理手柄与震动、长时间串流、AMF/QSV和不同声卡。没有把测试输入消息送达等同于游戏兼容性验证。Linux/Android、内置穿透、HDR/AV1、多显示器、驱动替代与零拷贝为后续工作。
