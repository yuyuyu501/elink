# Windows 原生媒体链路路线

## 当前基线

当前接收端路径是：

```text
H.264 RTP -> D3D11VA 或软件解码 -> PyAV to_ndarray(rgb24)
           -> 有界最新帧 -> QImage 外部 RGB 缓冲 -> Qt 绘制
```

`to_ndarray` 仍会把解码结果转换为 CPU RGB。播放器已经保留 numpy 缓冲的所有权并直接包装为 `QImage`，因此去掉了原先的 `QImage.copy()`；这降低了一次内存带宽消耗，但不是 GPU 零拷贝。运行时分别记录 `decode_ms`、`convert_ms` 和 `present_ms`，便于在不同设备上定位瓶颈。

## Windows 实现顺序

1. 保留 Python 控制平面，继续负责 WebRTC、信令、输入、设备发现、设置、更新和 Qt 页面。
2. 增加稳定的原生媒体接口（优先 C++ DLL，必要时可用 Rust/C ABI）：创建解码器、提交 H.264 包、获取 D3D11 texture、释放 surface。
3. 首先实现 D3D11VA/NVDEC 到 D3D11 texture 的接收路径，再接入 Qt RHI/Direct3D 纹理绘制，避免 `to_ndarray` 和 CPU RGB 回读。
4. 保留当前 CPU RGB 路径作为回退和自动化测试后端。设备初始化失败、远程桌面环境或驱动不支持时仍能显示画面。
5. 发送端随后把 DXGI texture 直接交给 NVENC，减少 DXGI 到 CPU 再回到 GPU 的往返。

D3D12 不作为第一实现目标。它可以在 D3D11 路径稳定后用于实验和性能对比，但需要额外的资源状态管理、共享句柄和 Qt/解码器互操作代码，不能仅通过切换 API 获得更低延迟。

## 其他平台

Android 阶段再单独实现 MediaCodec 的 `Surface` 输出和 Android 图形合成；它与 Windows 的 D3D11 surface 接口保持协议层兼容，但不共享平台图形代码。当前 Windows 版本不会为了 Android 提前引入移动端渲染抽象。

## 不采用的方案

不把画面切成 16×9 的 144 路独立编码任务。这样会增加码流、同步、丢包恢复和合成开销，无法保证每块独立到达就能立即显示；现代 GPU 解码器已经在帧内和帧间使用并行硬件。

## 带宽与 QoS

码率设置是目标值。编码器尽量使用该值，CBR/VBV 和帧级 pacing 控制突发；WebRTC 的 REMB 拥塞反馈在带宽不足时降低目标，恢复后缓慢升回。控制端和被控端都会尝试给 ICE UDP socket 设置 AF41 DSCP。该标记只影响支持 QoS 的本机网卡、交换机或路由器，不能跨 Tailscale/DERP 保证优先级，也不会保留 30 Mbps 的专用通道。需要本机 Windows 策略时，可由管理员在两台电脑分别运行 `scripts/windows_qos.ps1`；应用不会自动创建持久策略。
