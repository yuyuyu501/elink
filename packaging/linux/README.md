# Linux Preview

Build on the target Linux x86_64 system using `python -m scripts.package_linux`.
The complete .deb includes Python, Qt and media libraries. The current build
baseline is Deepin 25, glibc 2.38, X11; it is not an Ubuntu-wide compatibility
claim. Install with the system package manager and launch `elink` in a logged-in
desktop session as the regular user.

Wayland Portal capture/input is experimental and requires python3-gi,
gir1.2-gstreamer-1.0, gstreamer1.0-pipewire and a compatible RemoteDesktop Portal.
The compositor may require screen/input approval. This path has not been
validated on a physical Wayland desktop. The frozen X11 package does not bundle
GObject introspection; use a source environment with system GI for this path.

NVENC requires the installed NVIDIA driver's libnvidia-encode.so.1; CUDA decode
requires libnvcuvid.so.1. Missing components select software fallback. The app
does not install graphics drivers. The current user needs access to /dev/uinput
for virtual gamepads; the app never changes its permissions or runs as root.

Text clipboard requires xclip on X11. Linux updates detect Linux .deb assets and
use the system package manager for installation. Configuration/logs are stored
under XDG_DATA_HOME/elink (default ~/.local/share/elink), and autostart under
XDG_CONFIG_HOME/autostart/elink.desktop. TLS keys use user-only file permissions;
they are not encrypted by a desktop keyring in this Preview.
