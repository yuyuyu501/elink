# 90 FPS 调度修复计划与验证

日期：2026-10-15。目标配置：1080p、90 FPS、20 Mbps、NVENC、D3D11VA、GPU 纹理显示。

## 诊断与证据

- 本机显示器实测约 74.973 Hz。串流可以编码/接收 90 帧，但不能据此宣称屏幕每秒显示了 90 个完整、不同的画面。
- 修改前同机合成画面 GPU 提交约 85 FPS；真实桌面无 GUI 回环约 44 FPS，另一次约 68 FPS。画面复杂度、系统负载和计时环境会影响结果，这不是稳定的 30 帧上限。
- 真实桌面 NVENC 编码中位数约 3 ms、D3D11VA 解码约 2 ms，不能仅凭硬件名称认定整条链路快。
- 计时探针：同进程 `asyncio.sleep(1/5/11.1 ms)` 中位数分别约 15.59/15.43/15.53 ms；工作线程 `time.sleep` 约 1.55/5.53/11.63 ms（包含线程切换）。90 FPS 帧预算仅 11.11 ms。此处是 Windows 事件循环等待粒度问题；本机 Python 3.13 的时钟本身已经使用 QPC。
- aiortc 1.14 的发送循环串行执行“取帧和编码 → 整帧逐包限速发送 → 下一帧”。真实桌面的发送阶段中位数约 7.7 ms、P95 15.5 ms，与编码耗时相加容易超预算。仅诊断时关闭 pacer，真实桌面约 86.5 FPS；不能把取消流量整形作为正式修复。
- 修改前采集源是 BGRA；DXCam 先转 BGR，再拷入 PyAV 并转 YUV。还有可避免的转换/内存拷贝。旧编码 VBV 是半秒，允许过大的关键帧突发。

以上证明本机存在调度瓶颈，但尚不能断言用户两机测试的全部损失都来自同一原因。两机链路带宽、游戏负载仍需实测。

## 资料核对

1. [GamingAnywhere，MMSys 2013 论文](https://gaminganywhere.org/pub/2013mmsys.pdf)及[官方论文目录](https://gaminganywhere.org/doc.html)：按采集、转换、编码、网络、显示分段测量和验证。已获取官方 PDF；不引用未复核的性能数字。
2. [Salsify，NSDI 2018 作者项目页及 FAQ](https://snr.stanford.edu/salsify/)：编码速率与发送控制互相影响，需联合考虑。论文 PDF 端点本次返回 403，结论依据作者公开说明。其可回滚 VP8 编码器不能直接替换 NVENC；不能随意丢弃已编码 H.264 参考帧。
3. [Moonlight 有界帧队列与呈现调度](https://github.com/moonlight-stream/moonlight-qt/blob/master/app/streaming/video/ffmpeg-renderers/pacer/pacer.cpp)：限制排队数量，区分呈现节奏与接收帧率。
4. [Sunshine 视频流水线](https://github.com/LizardByte/Sunshine/blob/master/src/video.cpp)：采集、编码、包发送分阶段；低延迟 VBV 与帧预算关联。仅参考设计，不复制实现，也不引入其应用依赖。
5. [Microsoft Desktop Duplication](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api)：原生桌面表面为 BGRA，支持 GPU 处理。本轮仍是 CPU 可访问帧，不宣称 GPU 零拷贝。
6. [Python asyncio 平台注意事项](https://docs.python.org/3/library/asyncio-platforms.html)：Windows 定时限制；本文以本机等待实验补充版本相关差异。

## 实施顺序与边界

1. 加入可重复基准：固定运动图案与真实桌面两类输入，记录实际编码/解码/提交数、阶段 P50/P95、有效码率。不保存用户桌面图像，不用重复刷新 UI 或填充流量伪造 FPS。
2. 把帧截止时间等待放入已有单线程采集执行器，用高精度相对等待和绝对时间节奏；超时跳过错过的时间槽，不追赶式突发。时间戳在取到帧之后生成，增加调度超时统计。
3. 采集直接保留 BGRA，PyAV 引用拥有独立生命周期的 numpy 缓冲，复用颜色转换器。鼠标合成兼容 BGRA，保留不可修改缓存和无鼠标残影约束。
4. 增加会话级、最多一帧的编码预取，与当前帧发送重叠。H.264 帧保持顺序，不丢弃已编码参考帧；取消、错误、断开时清理预取任务。不修改安装包中的第三方库。
5. 将低延迟 VBV 绑定到两帧预算，改进 RTP 小批次整形，保留用户目标码率和有限突发；等待不阻塞输入事件循环，不累计无限发送队列，不声称硬性预留公网带宽。
   复测发现 NVENC 的 VBV 不是硬性帧大小上限：复杂图案 IDR 仍可占约 54 ms 媒体预算。参考 Sunshine 按需 IDR，NVENC 的周期关键帧从每秒改为每 10 秒兜底，保留 PLI/FIR 立即请求恢复；其他编码器不改变 GOP。增加强制 IDR 能独立解码、真实 RTCP 请求后继续解码的验证。
6. 第一轮完整 GUI 基准还发现：解码约 82 FPS 时提交仅约 65 FPS，8 ms 轮询与绘制调度仍会损失帧。增加有界的新帧到达通知，代替视频轮询；保留输入/状态定时器。通知仅携带唤醒，不排队整帧，关闭窗口时解绑。
7. 回归：时间槽与时间戳、缓存寿命/颜色/鼠标、编码顺序与取消、pacer 预算及慢链路、GUI 通知合并与关闭、真实 WebRTC 解码连续性、GPU 黑屏测试。运行完整测试后按项目规则提交并同步 Git。

## 验收

- 固定输入 1080p90/20 Mbps 本机 GPU 路径目标达到至少 85 实际编码/解码 FPS；报告未达标结果，不用设置值代替实测。
- 真实桌面与 60 FPS 对照；报告共享机器负载和桌面内容差异。物理扫描输出不属于 Qt 提交计数。
- 编码最多预取一帧；pacer 保持预算约束；错误和取消能结束会话。不得破坏输入、颜色、光标和 GPU 窗口显示。
- 不更改被控端屏幕分辨率/刷新率，不降低用户设置，不引入静态桌面降帧策略。
- 本次项目规则为本地修改、测试、Git 同步；不自动打包或发布新版本。

## 实施结果

上述七项已落实。未更改用户设置、系统显示模式或静态桌面帧率策略；没有取消 pacer、填充流量或将重复重绘算成新视频帧。编码预取适配项目固定的 aiortc 1.14.0 私有接口，升级依赖时必须重新核对；不修改 site-packages。

### 固定运动画面对照

本机 RTX 4070 SUPER，显示器约 74.973 Hz；1080p90、20 Mbps、NVENC、D3D11VA、GPU 显示。使用固定种子的纹理平移图案，预热 3 秒后测量。主机与控制端在同一台机器的独立 Python 进程中运行，共享 CPU/GPU。旧版为修改前提交 `d91cf4e` 的应用代码，使用同一基准脚本、输入与参数；运行未与其他基准并发。

| 测试 | 实际编码 FPS | 解码 FPS | 绘制提交 FPS | 有效编码载荷 Mbps |
| --- | ---: | ---: | ---: | ---: |
| 修改前，15 秒 | 60.54 | 60.07 | 57.40 | 旧版没有计数 |
| 修改后，15 秒 | 88.40 | 87.93 | 87.73 | 19.64 |
| 修改后，30 秒复测 | 89.10 | 88.86 | 87.83 | 19.80 |
| 修改后，60 FPS 设置，15 秒 | 59.40 | 58.93 | 58.87 | 19.80 |

这些运行均无解码错误。30 秒复测 GUI 邮箱覆盖约 0.93 帧/秒，说明仍有 GUI 调度损失；不以平均值宣称每一秒都锁定 90 FPS。15 秒对照中，GUI 邮箱等待 P50/P95 从约 3.32/7.36 ms 降到 0.10/0.30 ms；编码调用 P50 约 3.80 → 2.09 ms。阶段分位数保留最近 600 个样本，不代表整段测试所有帧。

另有两端共用同一 Python 进程的实验：旧版约 68.38 解码 / 63.68 提交 FPS，新版约 84.38 解码 / 84.31 提交 FPS。它额外受到同一 GIL 与运行时调度竞争影响，不能与分进程数字直接混用。

### 真实桌面与正确性

真实桌面、分进程、同样 1080p90/20 Mbps 的 15 秒回环：编码约 83.13、解码 83.06、提交 82.26 FPS，无解码错误，有效载荷约 18.48 Mbps。此测试会采集正在显示的播放器，内容未标准化，不能作为严格画质或游戏性能对照。采集读取/光标阶段 P50 约 7.45 ms、颜色转换约 3.02 ms，仍接近 11.11 ms 帧预算；GPU 解码表面共享和原生采集转换属于后续优化空间，本轮没有实现零拷贝。

GPU 原生输出验证通过：五组 YUV/NV12 色彩/范围检查，实际屏幕与内部像素最大 RGB 通道误差为 1–2/255；默认状态栏、软件鼠标、全屏往返、真实软件 H.264/D3D11VA 串流显示和注入故障后的 CPU 回退均通过。保留 OpenGL 3.3 兼容配置，避免旧黑屏配置回归。

自动回归覆盖绝对时间节奏、提前唤醒/错过时间槽、线程与 PTS、numpy 缓冲寿命/通道/步长、BGR/BGRA 光标、编码预取顺序/上限/取消、pacer 有限突发/空闲/延迟唤醒后的速率、通知合并与解绑。NVENC 强制 IDR 可独立解码，实际 WebRTC 的 RTCP PLI 请求可到达编码器并继续显示。其他厂商硬件的 VBV 行为没有在本机验证。提交时由 `scripts.check_and_sync` 执行完整测试，失败即停止同步。

### 复现与后续验收

```powershell
.venv/Scripts/python.exe -m scripts.benchmark_stream --present --separate-host --seconds 30 --output .artifacts/stream-motion.json
.venv/Scripts/python.exe -m scripts.benchmark_stream --source desktop --present --separate-host --seconds 15 --output .artifacts/stream-desktop.json
.venv/Scripts/python.exe -m scripts.benchmark_stream --fps 60 --present --separate-host --seconds 15 --output .artifacts/stream-60.json
.venv/Scripts/python.exe -m scripts.check_gpu_video --frames 30 --stream-check --output .artifacts/gpu-validation.json
```

报告只存计数和阶段摘要，不保存桌面像素、输入或身份。完整 JSON 留在忽略目录。基准中的 `presented_frames` 是视频帧提交绘制数，不是物理扫描数；桌面内容本身可能重复，75 Hz 显示器不因此变成 90 Hz。

仍需用户两端使用相同游戏场景验收并导出性能报告。此轮已达到固定输入至少 85 FPS 的目标，没有证明用户 WAN 链路能稳定 90 FPS，也没有硬性预留带宽。按当前项目规则仅测试和 Git 同步，不创建版本、EXE 或 Release。
