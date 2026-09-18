# Elink 自主虚拟手柄驱动调研

日期：2026-09-14。来源为当日 GitHub 默认分支源码、项目许可证与 Microsoft 文档。新参考项目未在本机安装或进行游戏兼容性测试；后续已实现并编译 ElinkPad 原型，进度见文末。

## 用户目标与结论

用户希望摆脱停止维护的 ViGEmBus 外部依赖，转向项目自己维护的驱动。该目标可行，但应区分“自主驱动”与“所有逻辑都放进内核”。现有参考包含 KMDF 内核驱动和 UMDF2 用户态驱动。

对 Elink 游戏串流而言，第一验收目标是游戏能通过 XInput 识别虚拟 Xbox 手柄，并正确收发状态、震动和热插拔。普通 HID 设备出现在 joy.cpl 中，不等于完成 XInput 兼容。VHF 本身也不是可直接调用的通用虚拟 XInput API。

## Moonlight 的映射逻辑

Moonlight Qt 的客户端使用 SDL GameController 接口打开本地手柄，统一不同品牌的按键和轴，维护设备索引与连接状态。映射管理支持保存的用户映射、SDL 配置和更新的映射数据。

`SdlInputHandler` 通过 `LiSendMultiControllerEvent` 等 moonlight-common-c 接口发送控制器索引、活动掩码、按键位图、左右扳机和四个摇杆轴；支持的扩展另有触摸、运动传感器和电池事件。主机反馈经客户端 SDL 的 rumble / haptic 接口作用于实体手柄。

这部分是客户端用户态输入处理，不是主机的虚拟手柄驱动。Sunshine 在 Windows 主机接收输入后，由其驱动后端生成虚拟设备并提交报告，再将输出报告中的震动等反馈传回 Moonlight。

源码入口：

- [Moonlight Qt gamepad.cpp](https://github.com/moonlight-stream/moonlight-qt/blob/master/app/streaming/input/gamepad.cpp)
- [Moonlight Qt mappingmanager.cpp](https://github.com/moonlight-stream/moonlight-qt/blob/master/app/settings/mappingmanager.cpp)
- [moonlight-common-c InputStream.c](https://github.com/moonlight-stream/moonlight-common-c/blob/master/src/InputStream.c)
- [Sunshine Windows input.cpp](https://github.com/LizardByte/Sunshine/blob/master/src/platform/windows/input.cpp)
- [SDL_GameControllerDB](https://github.com/mdqinc/SDL_GameControllerDB)，Zlib 许可证，提供实体手柄映射数据，不提供虚拟设备。

当前 Elink 控制端仅轮询 XInput；0.4 主机保留 ViGEmClient 兼容后端，并新增 ElinkPad 实验后端。未来扩展到 SDL 输入层，可增加非 XInput 实体手柄支持；这项工作与替换主机驱动独立。

## GitHub 参考

| 项目 | 已核实内容 | 对 Elink 的意义 |
| --- | --- | --- |
| [nefarius/ViGEmBus](https://github.com/nefarius/ViGEmBus) | 已归档，BSD-3-Clause，KMDF 内核驱动，Xbox 360 / DualShock 4 模拟 | 内核路线最直接的参考；`sys/XusbPdo.cpp`、`sys/EmulationTargetPDO.cpp`、`sys/busenum.cpp` 展示 XUSB、子设备与总线生命周期。维护派生实现可行，但必须保留许可证、重新测试与签名，不能借用上游签名。 |
| [microsoft/DMF](https://github.com/microsoft/DMF) | MIT，Microsoft Driver Module Framework；提供 `VirtualHidDeviceVhf` 模块 | 学习驱动资源、回调和 VHF 生命周期。不是现成 Xbox/XInput 驱动。 |
| [microsoft/Windows-driver-samples](https://github.com/microsoft/Windows-driver-samples) | Microsoft 官方 WDK 示例，仓库标注 MS-PL | 参考驱动框架与 HID 报告；`hid/vhidmini2` README 明确是 UMDF 示例，不应误称 KMDF Xbox 驱动。 |
| [hifihedgehog/HIDMaestro](https://github.com/hifihedgehog/HIDMaestro) | 未归档，近期有更新；LICENSE 使用 MIT 文本；README 描述 UMDF2、XInput/DirectInput 等接口和反馈通道 | 值得做现代用户态驱动的可行性验证。README 的设备数量、延迟和全 API 兼容声明不是 Elink 实测结果；需要审查实现与发行签名流程，不能直接当作性能结论。 |
| [cgutman/WinUHid](https://github.com/cgutman/WinUHid) | MIT，未归档；包含驱动、用户态库、设备定义和单元测试工程 | 可参考通用虚拟 HID 的分层与接口。仓库未提供 README，本轮没有验证其具体控制器兼容范围。 |
| [LizardByte/libvirtualhid](https://github.com/LizardByte/libvirtualhid) | Sunshine 当前作为子模块引用；平台中立 C++ API；Windows 有 UMDF 驱动、broker 和 Xbox 360 XUSB 软件设备实现 | 参考 Sunshine 新后端的设备/报告/反馈抽象。Windows 驱动与 broker 采用 LB-SAL 1.0，不能因公共库是 MIT 就当作全部 MIT 代码直接复用。 |
| [jshafer817/vJoy](https://github.com/jshafer817/vJoy) | MIT，虚拟摇杆项目，API 显示最后推送为 2023-08-17 | 可参考 HID/摇杆报告，但本轮不将其视为维护活跃的 Xbox/XInput 替代。 |

`libvirtualhid` 许可证以 [license-map.md](https://github.com/LizardByte/libvirtualhid/blob/master/LICENSES/license-map.md) 为准：跨平台库及未单列部分 MIT；Windows driver、broker、部分授权实现和生成安装包采用单独的源码可见许可证。项目 README 明确 Windows 虚拟手柄、键盘及 Raw Input 鼠标创建需要许可证。

Sunshine 当前 Windows `input.cpp` 同时包含 ViGEm 与 `libvirtualhid` 后端，不能概括成“Moonlight/Sunshine 只用 ViGEmBus”。主分支事实也不代表所有用户已安装版本都包含相同后端。

## 推荐架构

```text
Elink 控制端：SDL 或 XInput -> 统一手柄状态
  -> Elink 已有加密网络会话
  -> Elink 主机用户态服务：认证、校验、会话归属、设备索引
  -> 本地受限设备接口
  -> Elink 自主维护驱动：枚举设备、提交输入报告、接收输出报告
  -> Windows 输入栈 -> 游戏

游戏震动 -> 驱动输出报告 -> 主机服务 -> 网络 -> 控制端实体手柄
```

网络、证书、配对和媒体保留在用户态。内核路线下，只将确需内核的设备枚举和报告处理放入驱动；驱动不监听网络，不直接解析远端 JSON。设备接口需限制访问，报告长度与取值需验证，断开/进程退出时必须归零和回收虚拟设备。

优先完成一个 Xbox 360 兼容虚拟目标，然后扩展到四个目标、震动和会话隔离。DualSense、陀螺仪、自适应扳机等高级能力不作为第一版驱动的前置条件。

实现路线应先对照 ViGEmBus 的 KMDF/XUSB 实现与 HIDMaestro 的 UMDF2 方案做最小 XInput 实验。用户已经明确要求自主维护；是否需要内核态，应该由目标游戏和系统兼容性实测决定，而不是由“ViGEm 已停更”直接推导。

## 验证与发行条件

需要独立验证：XInputGetState 状态、XInputSetState 震动、多设备索引、拔插/进程异常退出、Windows 10/11、内存完整性与 Secure Boot，以及实际游戏兼容性。声称设备创建成功不能替代这些验证。

自编译内核驱动不继承 ViGEm 官方二进制的签名。正式内核驱动发行需要符合 Microsoft 当前的签名要求；开发测试签名也不能当作普通用户发行方案。第一轮驱动实验应在独立测试环境完成，不自动修改当前工作机的启动或签名设置。

Microsoft 资料：

- [VHF HID source driver](https://learn.microsoft.com/en-us/windows-hardware/drivers/hid/virtual-hid-framework--vhf-)
- [内核代码签名要求](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/kernel-mode-code-signing-policy--windows-vista-and-later-)
- [DMF VirtualHidDeviceVhf](https://github.com/microsoft/DMF/blob/master/Dmf/Modules.Library/Dmf_VirtualHidDeviceVhf.md)

后续更新已创建并编译 `native/elinkpad` UMDF2 驱动和临时软件设备助手，接入 0.4 主机后端选择。原生协议测试、应用测试、INF 校验与目录文件生成已完成；尚未签名、安装或完成 XInput/游戏实机验收，默认仍保留 ViGEm 兼容后端。详见 [ElinkPad 升级记录](elinkpad-upgrade.md)。
