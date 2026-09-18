# Third-Party Components

Elink 0.4 uses generic media, networking and UI libraries. Sunshine and Moonlight are architectural references only: neither application nor its source is bundled in this version. Elink is not affiliated with Sunshine, Moonlight, Tailscale or NetEase.

| Component | Version | License / source |
| --- | --- | --- |
| PySide6 / Qt | 6.10.2 | LGPL-3.0 / GPL-3.0 / commercial, component dependent; https://code.qt.io/ |
| aiortc | 1.14.0 | BSD-3-Clause; https://github.com/aiortc/aiortc/tree/1.14.0 |
| PyAV | 16.1.0 | BSD-3-Clause; https://github.com/PyAV-Org/PyAV/tree/v16.1.0 |
| FFmpeg and codec dependencies | PyAV wheel build | LGPL/GPL depending on configured libraries; https://ffmpeg.org/ and https://github.com/PyAV-Org/pyav-ffmpeg |
| DXcam | 0.0.5 | MIT; https://github.com/ra1nty/DXcam |
| SoundCard | 0.4.5 | BSD-3-Clause; https://github.com/bastibe/SoundCard |
| sounddevice / PortAudio | 0.5.3 / wheel build | MIT; https://github.com/spatialaudio/python-sounddevice |
| aiohttp | 3.13.2 | Apache-2.0 AND MIT; https://github.com/aio-libs/aiohttp |
| cryptography | 46.0.5 | Apache-2.0 OR BSD-3-Clause; https://github.com/pyca/cryptography |
| NumPy | 2.2.6 | BSD-3-Clause with dependency notices; https://numpy.org/ |
| OpenCV Python | 4.12.0.88 | Apache-2.0 and bundled dependency licenses; https://github.com/opencv/opencv-python |
| ViGEmClient | x64 DLL from vgamepad 0.1.0 source archive | MIT, Benjamin Höglinger-Stelzer; https://github.com/nefarius/ViGEmClient |
| HIDMaestro XUSB layout reference | 00b7303f8533c3fe10687a765c84e929b34a5e9c | MIT, 2026 HIDMaestro Contributors; https://github.com/hifihedgehog/HIDMaestro |
| Python / PyInstaller bootloader | Build-specific / 6.16.0 | PSF / GPL with distribution exception; https://python.org/ and https://pyinstaller.org/ |

ViGEmClient.dll was extracted without running setup.py from the pinned vgamepad source archive:
https://files.pythonhosted.org/packages/8a/54/0eaddc33f84247963af078f364b37153d09fcd6cdc398f243ec3e8842c56/vgamepad-0.1.0.tar.gz

Archive SHA-256: `57f6bd01aec0c172947517fb782d150ef9b285f7f4d524c317374fa5c24a89de`.
Only the client DLL is included, not vgamepad Python code, ViGEmBus installers or any driver. The ViGEmClient license is in `licenses/ViGEmClient-LICENSE.txt`.

Tailscale is separately installed and not bundled. Its service/account terms apply: https://tailscale.com/terms . No Tailscale account credentials are collected.

ElinkPad's experimental UMDF2 driver adapts XUSB wire layouts and capability arrays from HIDMaestro's `driver/companion.c`; the complete upstream notice is preserved in `licenses/HIDMaestro-LICENSE.txt`. No upstream driver binary or installer is included. The optional ElinkPad driver package is separate from the portable application and is unsigned. Microsoft WDK is a build-only dependency obtained from the pinned Microsoft NuGet package; its full toolchain is not redistributed. Windows' inbox WUDFRd is not redistributed. No LB-SAL-licensed libvirtualhid driver/broker source was used.

Dynamic Qt libraries remain replaceable in the folder build. The build script copies installed package license texts (including transitive dependencies such as OpenSSL, libsrtp, Opus, cffi and aioice) to `licenses/python-packages/` and records actual media-library versions. The PyAV FFmpeg binary includes libx264: review its actual GPL configuration, not only PyAV's BSD wrapper license.

This is a local development artifact. Public binary redistribution requires preparing the corresponding sources and notices for the actual GPL/LGPL dependencies and fulfilling their distribution terms. A URL alone does not satisfy every source-delivery obligation. No new license for Elink's own source is assigned here.
