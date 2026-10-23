# LAN device discovery

Elink lists online Elink applications, including controller-only Android devices.
It does not enumerate unrelated computers, printers or other devices without Elink.
Desktop hosts advertise `controller` and `host` roles with their HTTPS port.
Android advertises only `controller`, with port zero. Its device card is visible
but disabled and labelled as controller-only; discovery does not enable hosting.

Each active IPv4 LAN interface sends a directed broadcast from a socket bound to
that interface. A query includes compact presence metadata for the sender, so
either receiving a query or receiving a reply can discover a peer. Known peers
are also queried by unicast. Routes expire after 35 seconds without traffic and
are removed when they no longer belong to a local network. Stable device IDs
merge multiple addresses without merging distinct devices sharing a name.

Android listens on UDP 49201 while its activity is alive, acquires a Wi-Fi
multicast reception lock and announces every ten seconds. Discovery is not an
Android foreground service; background process suspension or termination can
make the device disappear until Elink resumes. Reinstalling or clearing app
data creates a new random discovery identity. No hardware ID is advertised.

Packets are limited to 2048 bytes, validated and restricted to permitted network
sources. Presence omits certificates and session details. This extends the
existing version-2 discovery query without changing media or session protocols.
Old desktop hosts remain discoverable, but both ends should update to obtain
reciprocal presence and controller-only device display.

## HTTPS presence fallback

Android also introduces its controller-only identity in the `X-Elink-Presence`
header of `/v1/info` requests to discovered and saved desktop hosts. A desktop
that accepts this header returns `presence_received=true` and advertises
`presence-https-v1`. The same desktop discovery cache, role validation, identity
merging and 35-second route expiry apply to UDP and HTTPS introductions.
The header contains Base64-encoded compact presence JSON, limited to 2048
decoded bytes, and is accepted only from the existing permitted network scope.
It neither starts an Android host nor authorizes a remote-control session.

While Android is resumed, it refreshes presence every ten seconds, including
during a stream. HTTPS probes prioritize live peers, use at most four concurrent
requests, have a four-second call timeout and an eight-second batch budget.
An older desktop ignores the header and remains connectable, but must update
to display a phone whose UDP announcements are not reaching it.

The Android home screen now separates Devices, Settings and About, with
controller-only cards and disabled connection actions. About can export the
app-private `filesDir/elink.log` and `elink.previous.log`, together with
interface, listener and learned-peer state. Failures include their stage,
message and exception stack. Logs rotate at 1 MiB and are not uploaded.

## Verification on 2026-10-23

An isolated Windows/Linux UDP test used real interfaces, with Ethernet and Wi-Fi
on different Windows subnets. Both endpoints discovered each other in two
successive rounds. Earlier packet tests showed a Wi-Fi broadcast asymmetry:
Windows broadcasts arrived on Linux, Linux unicast replies worked, but Linux
broadcast queries received no Windows response. Reciprocal presence plus learned
unicast probing recovered this case. The filtering location was not established;
this is not a claim to bypass complete client isolation or blocked unicast.

Automated coverage includes controller-only metadata and UI, identity merging,
route expiry, actual loopback UDP exchange and learned-peer unicast refresh.
Android unit tests and lint are required for the APK. Physical Android discovery
remains a user acceptance check; no USB-debuggable device is available.

The 0.5.5 HTTPS fallback was additionally checked on real Windows and Deepin
Linux interfaces using isolated test hosts. Each received an Android-format
controller-only introduction over HTTPS and returned one nonconnectable device
through desktop discovery after repeated introductions. These requests came
from the test computers, not a physical phone. This proves the desktop receive,
role handling and deduplication path; it does not establish the phone-specific
cause of the reported UDP failure. Android unit tests also cover diagnostic
exception export and home navigation at 320x640 and 640x360 logical pixels.
