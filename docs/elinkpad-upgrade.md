# Elink 0.4 手柄后端升级

日期：2026-09-14。用户目标：自主维护虚拟手柄，不依赖 Sunshine/Moonlight 应用，Windows 游戏串流优先。

## 已实现

- `native/elinkpad/driver.c`：Elink UMDF2/XUSB 实验驱动，单个 Xbox 360 风格目标。使用 Windows 现有 WUDFRd，不增加自有内核 `.sys`。自有硬件 ID，不匹配实体 Xbox 设备。
- `protocol.h`：版本化、固定长度的本地 IOCTL；控制权绑定 WDF 文件句柄；拒绝跨句柄写入、重复/过期序号和保留按键位；输入变化才增加 XInput 包序号。
- 驱动 500 ms 未收到有效报告时归零；释放控制权和文件清理同样归零。震动通过独立查询回传，XUSB 等待输入请求由 WDF 队列管理。
- `device.c`：显式启动的临时设备助手，唯一设备实例与 ContainerId，默认句柄生存期。助手退出移除设备；应用会话退出释放控制权和归零，但不会移除助手持有的设备。
- `elink/core/elinkpad.py`：直接访问设备接口，不加载 ViGEmClient；最新状态有界缓存，独立工作线程处理设备打开、提交和震动。500 ms 未收到新网络手柄快照时发送中立状态，避免后台刷新维持旧按键。
- 主机提供 ViGEm 兼容、ElinkPad 实验、禁用三种后端并保存选择。被控期间禁止修改，重启被控后应用新选择。明确选择 ElinkPad 时失败不会暗中回退 ViGEm；媒体和键鼠仍可工作。
- 默认保留 ViGEm 兼容模式。控制端仍是 XInput 采集，非 XInput 实体手柄的 SDL 映射不在本次驱动替换范围内。

## 本机验证

- 24 项 pytest 测试通过：原有 HTTPS/WebRTC 回环、身份验证与输入测试，新增延迟设备打开时只提交最新状态、关闭期间不提交旧输入、缺驱动不回退、震动协议校验、后端切换与设置恢复。
- MSVC 14.44、Windows SDK 10.0.26100.0、Microsoft WDK NuGet 10.0.26100.6584 编译成功；驱动导出 `FxDriverEntryUm`。
- 原生 C 测试直接运行与驱动共用的协议代码，覆盖布局、边界值、句柄归属、竞争、序号、反馈和超时释放。
- InfVerif `/u` 无错误；有一条 2084 警告：系统内置 WUDFRd 服务未在 CopyFiles 中提供。本包有意使用 Windows 自带反射器，不复制它。
- Inf2Cat 签名适用性检查无错误或警告，生成 `elinkpad.cat`。**生成目录文件不是完成签名。**
- 本机设备接口枚举为零：没有安装 ElinkPad、添加测试证书或修改启动/安全设置。
- 0.4 源码与便携 EXE 合成串流自测均通过：接收 91 个视频帧、76 个音频帧，NVENC 编码和 D3D11VA 解码，解码错误为零，声音播放已启动，运行线程已退出。这是本机短回环，不是端到端游戏延迟基准。
- Windows 原生 Qt 后端生成并检查 1120×800、900×680 主机页面，控件和中文说明无重叠。验证记录在 `.artifacts/packaged-selftest-v04` 和 `.artifacts/independent-ui`，不随软件分发。

便携程序：`dist/Elink/Elink.exe`。实验驱动独立压缩包：`dist/ElinkPad-0.4.0-experimental-windows-x64.zip`，含未签名 `.dll/.inf/.cat`、临时设备助手、源码、原生测试和许可证；不随主程序自动安装。

## 待验收与发行边界

本次产物是可编译、已接入应用的实验实现，还不能宣称已在游戏中替代 ViGEm。缺少已经配置好的独立驱动测试系统及发行签名，尚未运行真实 ElinkPad XInput/震动测试。`scripts/test_elinkpad_live.py` 提供独立测试系统的主动验收入口。

需要进一步验证：Windows 10/11 的 XInput 枚举、状态、震动、异常退出与热拔插、Secure Boot/内存完整性、目标游戏兼容。WGI、GameInput、DirectInput、浏览器 API 与四手柄均未承诺支持；不使用参考项目的非公开注册表兼容技巧。

设备 ACL 允许本地交互用户读写；控制句柄避免并发写入，但不是不同本地用户之间的完整隔离。正式产品仍需安装器、签名及受限的设备生命周期服务，不能要求最终用户长期手动运行管理员助手。

HIDMaestro 的 MIT XUSB 布局参考固定到 `00b7303f8533c3fe10687a765c84e929b34a5e9c`，保留完整许可证。未使用受 LB-SAL 限制的 libvirtualhid 驱动源码。构建和测试步骤见 [驱动 README](../native/elinkpad/README.md)。
