import socket
import threading
from zeroconf import Zeroconf, ServiceInfo, ServiceBrowser


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith('192.') or ip.startswith('10.') or ip.startswith('172.'):
                return ip
    except Exception:
        pass
    return '127.0.0.1'


SERVICE_TYPE = '_elink._tcp.local.'


class _DiscoveryListener:
    def __init__(self, discovery):
        self.discovery = discovery

    def add_service(self, zeroconf, service_type, name):
        self.discovery.add_service(zeroconf, service_type, name)

    def remove_service(self, zeroconf, service_type, name):
        self.discovery.remove_service(zeroconf, service_type, name)

    def update_service(self, zeroconf, service_type, name):
        pass


class Discovery:
    def __init__(self):
        self._zeroconf = None
        self._info = None
        self._browser = None
        self._running = False
        self._on_add = None
        self._on_remove = None
        self._lock = threading.Lock()
        self._peers = {}
        self._service_map = {}

    @property
    def peers(self):
        with self._lock:
            return dict(self._peers)

    def start(self, port, on_add=None, on_remove=None):
        if self._running:
            return
        self._running = True
        self._on_add = on_add
        self._on_remove = on_remove

        ip = get_lan_ip()
        hostname = socket.gethostname()
        self._zeroconf = Zeroconf()

        self._info = ServiceInfo(
            SERVICE_TYPE,
            f'{hostname}-{port}.{SERVICE_TYPE}',
            addresses=[socket.inet_aton(ip)],
            port=port,
            properties={'hostname': hostname},
        )
        self._zeroconf.register_service(self._info)

        self._browser = ServiceBrowser(
            self._zeroconf,
            SERVICE_TYPE,
            _DiscoveryListener(self),
        )

    def stop(self):
        self._running = False
        if self._zeroconf:
            if self._info:
                try:
                    self._zeroconf.unregister_service(self._info)
                except Exception:
                    pass
                self._info = None
            self._zeroconf.close()
            self._zeroconf = None
        self._browser = None
        with self._lock:
            self._peers.clear()
            self._service_map.clear()

    def add_service(self, zeroconf, service_type, name):
        if not self._running:
            return
        info = zeroconf.get_service_info(service_type, name)
        if info is None:
            return
        if not info.parsed_addresses():
            return

        ip = info.parsed_addresses()[0]
        port = info.port
        key = (ip, port)
        own_ip = get_lan_ip()
        if ip == own_ip:
            return

        hostname = info.properties.get(b'hostname', b'').decode('utf-8', errors='replace')
        if not hostname:
            hostname = info.server.split('.')[0] if info.server else ip

        with self._lock:
            self._peers[key] = hostname
            self._service_map[name] = key

        if self._on_add:
            self._on_add(ip, port, hostname)

    def remove_service(self, zeroconf, service_type, name):
        if not self._running:
            return
        peer_name = ''
        with self._lock:
            key = self._service_map.pop(name, None)
            if key:
                peer_name = self._peers.pop(key, '')
        if key and self._on_remove:
            self._on_remove(key[0], key[1], peer_name)
