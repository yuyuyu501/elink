# Elink 串流优化：论文与工程参考

整理日期：2026-10-14。以下是可借鉴的研究方向，不是本次已经实现的性能成果，也不是当前领域的完整综述。论文年代较早，适合参考架构和实验方法；具体显卡兼容性应以当前驱动、SDK 和实测为准。

## 1. GamingAnywhere：拆分并测量完整云游戏链路

- Chun-Ying Huang 等，**GamingAnywhere: An Open Cloud Gaming System**，ACM MMSys 2013。
- 后续扩展：**GamingAnywhere: The First Open Source Cloud Gaming System**，ACM TOMM 10(1s)，2014。
- [官方项目与设计说明](https://gaminganywhere.org/)，[官方论文目录](https://gaminganywhere.org/doc.html)。项目设计强调采集、编码、传输等模块可替换，支持研究者复现实验。

对 Elink 的建议：分别记录采集/颜色转换、编码、发送排队、接收重组、解码、颜色回读、绘制的耗时与丢帧。以 P50/P95/P99 和帧间隔分布判断卡顿，避免只看平均 FPS。当前代码虽然有 capture_ms、encode_ms、convert_ms、present_ms，但不是完整逐帧跟踪；present_ms 也未覆盖全部窗口缩放成本。

不建议直接换成这套老运行时。借鉴模块边界和测试方法即可，Windows 图像路径仍应采用当前驱动与硬件 API。

## 2. Salsify：让编码预算与网络发送协同

Sadjad Fouladi 等，**Salsify: Low-Latency Network Video through Tighter Integration between a Video Codec and a Transport Protocol**，USENIX NSDI 2018，267–282。

- [斯坦福项目页、实验说明与 FAQ](https://snr.stanford.edu/salsify/)
- [论文](https://www.usenix.org/system/files/conference/nsdi18/nsdi18-fouladi.pdf)
- [研究实现](https://github.com/excamera/alfalfa)

该研究把逐帧编码决策和逐包传输控制放在同一控制环中，减少编码器速率控制与传输拥塞控制相互滞后造成的排队。其原型使用可保存/恢复内部状态的 VP8 编码器，能选择不同编码结果或丢弃已编码帧；不能直接照搬到当前 NVENC + H.264。

对 Elink 的建议：保留用户目标码率和目标帧率作为优先目标，监测发送队列时间、交付速率和 RTT 上升，仅在真实拥塞时下调编码预算，恢复后再回到用户目标。静态画面的低有效流量本身不应被当成带宽不足。硬件 H.264 下不要随意丢弃已编码参考帧，应优先在编码前淘汰过期采集帧，或以关键帧恢复为配套。

原论文策略包含跳帧，与用户偏好的“尽量保持目标帧率”不完全一致，因此建议采用协同预算和限制排队的思想，而不是移植整套控制器。本文不建议强制每帧重复编码，也不建议伪造填充流量来让 Mbps 数字等于目标值。

## 3. BBR：参考带宽、基础 RTT 和排队的区分

Neal Cardwell 等，**BBR: Congestion-Based Congestion Control**，ACM Queue，2016；相同内容发表于 CACM，2017。属于拥塞控制技术文章，并非专门研究游戏串流。

- [原文](https://queue.acm.org/detail.cfm?id=3022184)
- [Google 官方代码与文献索引](https://github.com/google/bbr)
- [官方 FAQ](https://github.com/google/bbr/blob/master/Documentation/bbr-faq.md)

原文站点此次返回 403；年份、文章对应关系和实现入口已通过 Google 官方仓库核对。建议参考交付速率、最小 RTT 和 pacing 的概念，将路径固有时延与拥塞排队区分开，而不是仅用一个瞬时吞吐值控制码率。

当前 Elink 使用 WebRTC / RTP / UDP；启用 Linux TCP BBR 不会自动改变这条媒体通道。在 Windows 上也不是安装一个库就能给 Elink 硬性预留网络带宽。可借鉴估计方法，但若更换传输控制器，必须单独实现反馈、验证公平性和拥塞恢复，不能宣称已启用 BBR。

## 4. 端到端延迟测量，而不是 RTT 加解码时间

Kuan-Ta Chen 等，**Measuring The Latency of Cloud Gaming Systems**，ACM Multimedia 2011，见 [官方论文目录](https://gaminganywhere.org/doc.html)。此次确认了书目信息，未把未核实的论文实验数值用于性能结论。

针对当前项目，建议设置可重复的键鼠触发画面变化，用高速摄影或同一时钟的硬件采集测输入到显示的总延迟。Salsify 项目页也公开描述了为每帧加入条码、使用同一硬件时钟给源和接收画面打时间戳的实验方法。跨机器的两个 perf_counter 时间戳不能直接相减；RTT + decode_ms 也不是端到端操作延迟。

## 工程对照：Moonlight

[Moonlight Qt 源码](https://github.com/moonlight-stream/moonlight-qt) 是实践参考，不是论文。官方 README 明确支持硬件解码、多种视频编解码器和 YUV 4:4:4。直接显示 GPU 解码表面、把 YUV→RGB 转换与缩放留在 GPU 上，是比不断优化 numpy/QImage 拷贝更值得投入的长期方向。

当前 Elink 仍通过 PyAV 解码、to_ndarray(BGR) 和 Qt QWidget 显示；选择 D3D11VA 不等于已经实现端到端 GPU 零拷贝。建议保留 Python 设备管理/设置 UI，用独立原生模块承接 D3D11 解码表面和呈现，并保留现有路径作回退。引入开源代码前需检查相应许可证；本次只参考设计，没有复制第三方实现。

## 推荐实施与验收顺序

1. 建立固定测试画面：小号文字、彩色文字、滚动网页、快速镜头运动，固定分辨率、帧率、码率和网络条件。记录各阶段 P95、帧间隔、过期帧率及输入到显示延迟。
2. 做 GPU 纹理呈现原型，与当前 BGR/QImage 路径 A/B 对比 CPU 使用率、复制次数、P95 和画质；无收益则保持回退，不单凭“硬解”名称判优。
3. 以真实发送排队和交付反馈改进 pacing/拥塞恢复；测试稳定带宽、突降、恢复、突发丢包和 Tailscale 中继，检查是否能回到用户目标。
4. 在同尺寸、无二次缩放的图像上比较编码质量。先排查尺寸、采样滤镜、颜色矩阵/范围，再试 NVENC 更高质量 preset、HEVC 或 4:4:4，分别测延迟和带宽代价。

不优先用 540p 超分来解决 1080p 桌面文字毛糙：输入阶段已经丢失的细小文字和彩色边缘信息不能保证被还原。GPU 渲染、正确采样与颜色处理、编码质量对比更直接。FSR 等超分适合作为后续可选模式，需要独立验证动态伪影和文字可读性。
