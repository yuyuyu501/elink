# 第三方声明

## Lucide 图标

`frontend/desktop/resources/lucide/*.svg` 来自 [Lucide](https://lucide.dev)（v0.468.0），
以 ISC License 授权。运行时会按主题色重写 `stroke` 属性，其余内容保持原样。

## Inno Setup 简体中文语言包

`packaging/windows/ChineseSimplified.isl` 为 Inno Setup 官方简体中文语言文件，
仅在构建 Windows 安装包时使用。

## ffmpeg

Windows 发布包在 `ffmpeg/` 中携带 ffmpeg（含 `libx264` 与 `libsrt`），
同时保留所用发行版的许可证及说明文件。源码运行时使用配置路径或系统 PATH。

## PySide6 / Qt

桌面端界面基于 PySide6（LGPLv3）。随包分发时 Qt 库以动态库形式提供，
并保留其许可证文本。

## ViGEmClient / ViGEmBus

`frontend/desktop/resources/ViGEmClient.dll` 来自
[ViGEmClient](https://github.com/nefarius/ViGEmClient)，MIT License
（Copyright (c) 2018 Benjamin Höglinger-Stelzer），许可证全文见
`licenses/ViGEmClient-LICENSE.txt`。

应用只分发客户端 DLL，不打包 ViGEmBus 驱动安装器；手柄注入需要目标机器
已安装 ViGEmBus（由用户自行安装并接受其许可证）。

## Inno Setup

Windows 安装包由 [Inno Setup](https://jrsoftware.org/) 生成；安装器本身不随
应用分发。构建时使用的简体中文语言文件来自 Inno Setup 官方仓库。
