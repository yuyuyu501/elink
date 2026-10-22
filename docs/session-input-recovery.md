# Windows session input and capture recovery

## Evidence and limits

Two different failures were observed in 0.5.0:

- A controller lost heartbeat responses while video had recently been healthy.
  Existing logs do not establish the original cause. Synchronous native clipboard
  writes in data-channel callbacks can block heartbeat processing; these writes
  now run outside the network loop. Bounded heartbeat recovery and independent
  HTTPS probes improve resilience and distinguish later failures.
- During a UAC transition, native D3D11 acquisition returned permission errors,
  and DXCam initialization returned E_ACCESSDENIED. Another native reproduction
  returned AVERROR_EXTERNAL from an initialized ddagrab reader. Previously these
  exceptions ended RTP production, followed by a 15-second video timeout.

The capture worker now retries permission failures, DXGI desktop-switch errors
and AVERROR_EXTERNAL while reading an initialized D3D11 capture. It closes and
recreates that capture on its owning worker, retaining ordinary desktop/black
frames and advancing RTP timestamps while unavailable. Unsupported initialization
errors keep the existing fallback/failure policy. No secure-desktop access is
provided. Recovery of an already-running DXCam producer blocked internally in
its output-change loop is not established by this change.

The administrator restart is an explicit ShellExecute runas request, with the
replacement waiting for the previous instance to release its data lock. UAC
requires local user approval. The attempted elevated Task Manager integration
test did not complete; unit tests verify relaunch arguments, cancellation and
input diagnostics, not successful Task Manager control.

## Game pointer

The old boundary rule confined only fullscreen relative input, and explicitly
released windowed input outside the video. Raw Input reports hardware motion but
does not prevent the operating-system pointer reaching local UI. In addition,
stale visibility samples could change smart/local game input back to absolute.

Relative input now confines to the video/viewport intersection independently of
window state. Desktop absolute input remains free to leave and reenter. The last
confirmed cursor visibility survives missing samples; a new visible sample,
F8, focus loss, explicit release or disconnect releases confinement. This follows
the relative-input behavior documented by SDL and used in Moonlight.

## High-integrity input options

`SendInput` is the supported system-wide path for ordinary desktop and game input,
but Windows User Interface Privilege Isolation (UIPI) limits a medium-integrity
process from injecting into a high-integrity target. The older `keybd_event` and
`mouse_event` APIs are compatibility wrappers around the same input subsystem;
switching to either does not bypass UIPI. `PostMessage` and `SendMessage` can
place window messages in a target queue, but they do not provide system-wide
pointer movement, do not work for games that consume Raw Input or DirectInput,
and cannot reliably control arbitrary Task Manager UI.

The implementation therefore keeps `SendInput` for the normal path and starts a
short-lived, same-session elevated broker only after a high-integrity foreground
window causes a denial. A kernel input driver or a virtual HID driver could
cross more integrity boundaries, but it would require installation, signing and
additional security review; this project does not install such a driver. The
broker path is the next test target: confirm the UAC prompt, verify the broker
log and then test Task Manager on both updated Windows machines.

## Validation

- Native Windows CPU/GPU players: two sizes, 16:9 and 4:3 video, all three mouse
  modes, four boundaries, raw-input fallback, stale visibility, desktop reentry,
  F8, focus loss, and partially offscreen windows.
- Real RTC connection with simulated 16-second permission denial: video keeps
  arriving, the same peer connection recovers, both roles persist failures and
  exported reports include the failures.
- Real D3D11 acquisition before/after replaying the observed AVERROR_EXTERNAL:
  capture reconstructs and resumes with strictly increasing timestamps. This is
  a replay of the native error, not a completed UAC end-to-end acceptance test.
- Six-second suppressed pongs recover without reconnecting. A missing input
  heartbeat still releases held keys after two seconds. Slow native clipboard
  writes on both peers do not stop media or pongs.

Actual games, the original intermittent heartbeat fault and administrator-window
control still require acceptance on both updated applications. No driver was
installed and no security policy was changed.

## References

- [Microsoft SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput):
  UIPI allows injection only into equal/lower integrity targets; GetLastError
  does not reliably identify UIPI rejection.
- [Microsoft DuplicateOutput](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutput1-duplicateoutput):
  desktop access can be denied; ordinary administrator elevation is insufficient
  for secure-desktop acquisition.
- [Microsoft AcquireNextFrame](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-acquirenextframe):
  ACCESS_LOST requires releasing and recreating the duplication interface.
- [Microsoft ClipCursor](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-clipcursor):
  native pointer constraints must be released when yielding input control.
- [SDL relative mode](https://wiki.libsdl.org/SDL3/SDL_SetWindowRelativeMouseMode):
  focused relative input hides/constrains the pointer and provides continuous
  movement at boundaries.
- [Moonlight input implementation](https://github.com/moonlight-stream/moonlight-qt/blob/master/app/streaming/input/input.cpp):
  setCaptureActive enables relative mouse mode independently of fullscreen.
