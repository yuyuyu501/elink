"""Local interface inventory used by device discovery."""

from PySide6.QtNetwork import QNetworkInterface


def network_interfaces() -> list[dict]:
    flags = QNetworkInterface.InterfaceFlag
    return [
        {
            "index": interface.index(),
            "name": interface.humanReadableName(),
            "up": bool(interface.flags() & flags.IsUp and interface.flags() & flags.IsRunning),
            "loopback": bool(interface.flags() & flags.IsLoopBack),
            "can_broadcast": bool(interface.flags() & flags.CanBroadcast),
            "addresses": [
                {
                    "ip": entry.ip().toString(),
                    "prefix": entry.prefixLength(),
                    "broadcast": entry.broadcast().toString(),
                }
                for entry in interface.addressEntries()
            ],
        }
        for interface in QNetworkInterface.allInterfaces()
    ]
