# Android Controller Preview

Minimum Android 11 (API 30), arm64-v8a. This APK is a controller; it does not
share the Android screen as a host. Connect to Elink 0.5.0 on the same reachable
LAN or use a manually entered Tailscale IP/MagicDNS name. Tailscale is separate.

Uses Kotlin native Views, libwebrtc H.264/Opus and SurfaceViewRenderer/EGL.
Quality settings are filtered against the hardware decoder's declared size/rate
capabilities. That declaration is not a sustained performance guarantee.
Text clipboard transfer is explicit. External keyboard/mouse, pointer capture
and physical/basic virtual gamepad input are included for manual acceptance;
the virtual pad currently supplies buttons, not touch analog sticks.

## Development

Use JDK 21 and Android SDK 35 with build-tools 35.0.0:

```sh
./gradlew --no-daemon testDebugUnitTest lintDebug assembleDebug
```

The repository pins Gradle, AGP, Kotlin and WebRTC versions. SDK/JDK/Gradle
caches and signing keys are never committed. Production driver installation
and USB debugging are not required to install a downloaded Preview APK.

## Packaging

From a clean synchronized checkout on Windows, with JAVA_HOME/ANDROID_HOME set:

```powershell
.venv/Scripts/python.exe -m scripts.package_android
```

This generates or reuses a persistent local Preview signing key outside the
repository, runs Release unit tests/lint, verifies the APK signature, version
and ABI, then writes hashes and provenance into dist/android. Preserve that
signing identity for future in-place upgrades; do not publish it. Other build
hosts must provide ELINK_ANDROID_KEYSTORE and password environment variables
from secure storage. The Preview has no application updater yet.

No USB device test was performed in this development cycle. Android/desktop
interoperability, MediaCodec behavior, touch/physical input and thermal
stability still require installation on the target phone/tablet.
