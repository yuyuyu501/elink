# 前端

两套界面，共用同一个后端（`backend/`）：

| 目录 | 技术 | 形态 |
| --- | --- | --- |
| `desktop/` | **PySide6（Qt）** | 桌面应用，可打包成 exe / 安装包 |
| `mobile/` | 原生 ES Module + PWA | 浏览器访问，可加到主屏 |

`shared/` 是移动端与 `dev_server.py` 共用的协议编解码与传输层（桌面端用 Python，
不需要它们）。

## 桌面端（Qt）

```
frontend/desktop/
├── app.py            # 入口：QApplication、主题、单实例锁、--self-test
├── diagnostics.py    # 打包自检（界面 + 媒体链路）
├── service.py        # 与 backend 的桥：会话线程、控制通道、输入注入
├── settings.py       # 设置持久化（host/viewer/ui 三组 JSON）
├── startup.py        # 当前用户开机自启动（Windows Run 键）
├── discovery.py      # UDP 49320 同网设备发现
├── theme.py          # QSS + 调色板（UU 远程设计语言，主色 #2B6BFF）
├── icons.py          # SVG 图标按主题色着色并缓存
├── paths.py          # 源码/冻结两种运行方式的路径解析
├── resources/lucide/ # Lucide 图标（ISC）
└── ui/
    ├── window.py     # 主窗口：自绘标题栏 + 分组侧栏 + 页面栈 + 页脚 + 托盘
    ├── pages.py      # 总览 / 主机 / 观看 / 设备与发现 / 日志
    ├── settings_page.py # 设置：常规、发现、数据与诊断、关于
    ├── stream_window.py # 独立全屏播放器 + 悬浮控制栏
    ├── upscale.py    # FSR 1 / RTX VSR 超分（GL 离屏与 D3D11 原生 DLL）
    ├── preview.py    # 深色预览（BGRA → QImage，状态徽章 + HUD + F10 统计）
    └── widgets.py    # 卡片 / 胶囊 / 开关 / 提示条 / 指标 / 标题栏 / 英雄区
```

运行：

```powershell
# 需要 Python 3.11+ 与 PySide6
pip install -e .            # 或 pip install PySide6
python main.py --ui         # 桌面端
python main.py --self-test out/self-test.json
```

### 两种模式

- **主机（被控）**：`DesktopCapture` 采集 → 可选缩放 → 编码（libx264 / NVENC / QSV / AMF）
  → MPEG-TS → SRT 或文件；同时开 TCP 控制通道（默认 7000）接受远端键鼠并注入。
- **观看（控制）**：SRT/文件 → 解封装 → 解码 → 预览；开启「接管键鼠」后把本机
  Windows 低级钩子事件通过控制通道发给对方；可同步文本剪贴板、转发 XInput 手柄，
  开启「超分」后由 `upscale.py` 在 FSR 1（离屏 OpenGL EASU+RCAS）或 RTX VSR
（`elink_video.dll`）重建，
  F11 打开独立全屏播放器，F8 释放输入，F10 叠加连接统计。
- **主机**：可接收对端剪贴板文本；手柄事件通过 ViGEmBus 注入为虚拟 Xbox 360 手柄
  （需系统安装 ViGEmBus，未安装时不影响键鼠/串流）。

界面不直接调用 ffmpeg/Win32：全部经 `service.py` 在后台线程执行，跨线程用 Qt 信号回传。

### 数据目录

`%LOCALAPPDATA%\Elink`（Linux：`$XDG_DATA_HOME/Elink`），可用 `ELINK_DATA_DIR` 覆盖。
与旧版共用用户目录以便原地升级；新版设置落在 `config/desktop.json`，
不会覆盖旧版的 `desktop-v3.json`。

## 移动端（PWA）

```powershell
py -3.13 frontend/dev_server.py --port 8099        # 只预览，不注入
py -3.13 frontend/dev_server.py --port 8099 --allow-inject
# 手机浏览器打开 http://<本机IP>:8099/mobile/
```

- 单指拖动 = 左键拖动，双指点按 = 右键，双指上下拖动 = 滚轮
- 「键盘」按钮按文本注入（移动端键码不可靠，文本通道更稳）
- 画面是低频快照（默认 2fps / 480 宽），不是视频流
- 视觉与桌面端共用设计令牌（冷白底、白色卡片、亮蓝主色、深色预览、
  黑底在线胶囊）；设计说明见 `docs/UI_REDESIGN.md`

## 协议

移动端与 `dev_server.py` 复用 `backend/control_channel/protocol.py` 的帧格式：

```
0        1        2       3        4..7          8..
magic    version  type    reserved length(be32)  payload(UTF-8 JSON)
0xE1     1        1       0
```

桌面端不用这套 WebSocket 通道：它直接调用后端对象，并通过 `control_channel/tcp.py`
与另一台机器通信。

## 打包

见项目根 `README.md` 的「打包」一节；脚本在 `scripts/`，安装器脚本在 `packaging/`。
