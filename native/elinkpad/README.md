# ElinkPad 0.4 experimental

Elink-maintained UMDF2 function driver exposing one Xbox 360-style XUSB
interface and a private, versioned control interface. This is an experimental
driver, not a signed or game-compatibility-tested ViGEm replacement.
The INF targets Windows 10 build 19041 and later, x64 only; live validation
on those operating systems remains outstanding.
Windows' existing WUDFRd kernel reflector is used; Elink does not ship a new
kernel-mode `.sys` file. Network and authentication stay in the application.

## Build without installation

From the project root, with Visual Studio 2022 C++ Build Tools and Windows SDK
10.0.26100.0 installed:

```powershell
.venv/Scripts/python.exe scripts/build_elinkpad.py --fetch-wdk
```

The pinned Microsoft WDK NuGet is extracted under `.artifacts`, not installed
globally. The script compiles native protocol tests, the driver and temporary
device helper; runs InfVerif and Inf2Cat; and writes hashes under
`build/elinkpad`. Catalog creation is NOT signing. `/MT` embeds the native CRT.
InfVerif may emit warning 2084 for the inbox WUDFRd service: the reflector is
already supplied by Windows and is intentionally not copied into our package.
The script also writes `dist/ElinkPad-0.4.0-experimental-windows-x64.zip` with
the unsigned package, lifetime helper, source, native tests and license. The
Python live test requires the Elink source checkout and its environment.

## Isolated OS validation

Use a dedicated Windows driver test machine or VM and the Microsoft driver
test-signing process appropriate for that OS. There is deliberately no script
to change Secure Boot, test-signing settings or certificate trust. Once the
package has been appropriately signed and trusted in that test OS, stage it
with an elevated `pnputil /add-driver package/elinkpad.inf /install`.

Run `ElinkPadDevice.exe --hold` elevated in that OS. It creates a unique software
device with a unique ContainerId and default handle lifetime. It does not
install anything. Keep the console open while selecting ElinkPad in the app;
Enter or helper process exit removes this temporary devnode. SwDeviceCreate
success alone does not establish successful driver binding.

The app can acquire the existing device without elevation; it does not create
one automatically. A production installer and narrowly privileged lifecycle
broker remain future work. The helper intentionally keeps enumeration separate
from the app's control lease. Disconnecting a session neutralizes and releases
the lease; it does not remove the helper's devnode.

Validate XInput enumeration, every button/axis, negative axis limits, motor
ordering, lease contention, process kill, 500 ms input timeout, game launch and
helper removal before expanding compatibility. Test Windows 10/11, Secure Boot,
memory integrity and real games independently. WGI, GameInput, DirectInput,
browser Gamepad API and multiple virtual pads are not claimed supported.

From the source checkout in the isolated test OS, with the app session stopped,
run `python -m scripts.test_elinkpad_live --slot 0 --exercise-input`, replacing
0 with the known virtual device's XInput slot. This actively submits synthetic
input and vibration. It verifies state, rumble, competing handles, timeout,
release and reacquisition. It does not sign or install a driver. Do not guess
the slot or run it against a physical controller in a normal gaming session.

## Protocol and trust boundary

`protocol.h` defines packed little-endian v1 structures and private IOCTLs.
The control interface GUID is `654cd75a-921d-434f-94bb-807daa86b076`.
Query returns version, max pads and timeout; claim/update/poll/release are tied
to the same non-null WDF file object. Update sequences must increase and the
reserved button bit is rejected. The driver neutralizes state/rumble after
500 ms without valid input, on release and on file cleanup. All callbacks share
the device synchronization lock. Pending XUSB input waits use a manual queue
and complete on submission or an 8 ms timer, with WDF handling cancellation.

ACL grants System/Administrators full access and local interactive users
read/write access needed by XInput games. Network logon users and anonymous
users have no grant. This trusts local interactive applications: any such app
can win the first lease or send XInput rumble. The lease prevents simultaneous
writers, but is not a per-user security isolation or anti-malware boundary.

## Reference and license

XUSB response layouts and capability byte arrays were adapted from
[HIDMaestro companion.c](https://github.com/hifihedgehog/HIDMaestro/blob/00b7303f8533c3fe10687a765c84e929b34a5e9c/driver/companion.c),
MIT, copyright 2026 HIDMaestro Contributors. Preserve the full
`licenses/HIDMaestro-LICENSE.txt` (included alongside binary packages).
Rumble's five-byte layout also follows that commit's
`docs/investigations/wgi-silent-sink-2026-04/microsoft-questions/driver-dev-feedback.md`.
These are reverse-engineered implementation references, not Microsoft's public
XUSB contract. Their correctness on our target OS remains a live test gate.

No HIDMaestro binaries, shared-memory transport, global configuration,
`xinputhid` registry workarounds, or restricted libvirtualhid code are bundled.
