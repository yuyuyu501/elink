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
