# Elink v2 interoperability

HTTPS signaling on 49200; IPv4 UDP discovery on 49201. The media/control path is
WebRTC (H.264 video, Opus audio, DTLS/SRTP, SCTP DataChannels), with no ICE server
required on LAN or externally connected Tailscale peers.

- GET /v1/info returns protocol="elink", version=2, host_id, name, port, busy,
  features, and optional capabilities. Unknown optional capabilities are ignored.
- POST /v1/session receives {type:"offer", sdp, settings:{width,height,fps,bitrate,
  audio,render_scale}}; answer is {id,type:"answer",sdp}. SDP video/audio sections
  must be recvonly. Gather all ICE candidates before posting (no trickle route).
- POST /v1/session/{id}/settings applies stream dimensions/rate, not OS display
  configuration. DELETE /v1/session/{id} releases the host slot.
- control: ordered reliable JSON strings. motion: unordered maxRetransmits=0.
  JSON binary messages are not accepted.
- Send heartbeat {type:"heartbeat",epoch,active} reliably every 500 ms. Increment
  epoch when focus changes. Every input event carries the current epoch.
- move {x,y,absolute,seq}; absolute coordinates range 0..65535 relative to the
  displayed video, excluding letterboxing; relative deltas range -32767..32767.
- key {vk,scan,extended,down}; v2 uses Windows VK and set-1 scan semantics. New
  non-Windows controllers send semantic VK with scan=0. Do not send Linux X11
  keycodes or Android KeyEvent codes unchanged. HID usage is a future extension.
- button {button:"left"|"right"|"middle",down}; wheel {delta}, 120 per detent.
- pad {index:0..3,seq,values:[XInputButtons,LT,RT,LX,LY,RX,RY]}; LT/RT 0..255,
  sticks -32768..32767; send releases or heartbeat active=false on focus loss.
- clipboard {text} reliable, max 64000 characters, only where supported.
- ping {time} -> pong echoes the same value and reports actual encoder and
  cursor state. A controller's monotonic timestamp is never compared with a host
  clock; only the echoed value establishes RTT.
- Send {type:"disconnect"} reliably before DELETE and transport cleanup. Peer
  failures and missing heartbeats release inputs and eventually free the slot.

H.264 High is preferred between desktop peers; baseline remains negotiable with
native libwebrtc. The encoder must honor the negotiated profile. Displayed codec
names are actual backends. A hardware selection is not a claim of zero-copy.

HTTPS uses a self-signed device certificate. Desktop v2 fingerprints the peer
before signaling; Android additionally persists a first-use SHA-256 pin per
endpoint and rejects subsequent identity changes until the device is removed.
Hosts admit actual local interfaces, loopback and known Tailscale peers only.

capabilities describes platform, roles, capture/input backend names, cursor_modes,
clipboard, gamepad and gamepad_feedback. A missing object uses v2 Windows legacy
defaults. Wayland permissions and Android foreground/clipboard restrictions must
not be inferred from a successful signaling response.
