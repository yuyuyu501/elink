import struct
import json

MSG_CMD = b'CMD '
MSG_SCR = b'SCR '

def _recv_exact(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError('Connection closed')
        buf += chunk
    return buf

def send_cmd(sock, cmd):
    data = json.dumps(cmd).encode('utf-8')
    header = MSG_CMD + struct.pack('!I', len(data))
    sock.sendall(header + data)

def recv_cmd(sock):
    msg_type = _recv_exact(sock, 4)
    if msg_type != MSG_CMD:
        raise ValueError(f'Expected CMD, got {msg_type}')
    length = struct.unpack('!I', _recv_exact(sock, 4))[0]
    data = _recv_exact(sock, length)
    return json.loads(data.decode('utf-8'))

def send_screen(sock, jpeg_data):
    header = MSG_SCR + struct.pack('!I', len(jpeg_data))
    sock.sendall(header + jpeg_data)

def recv_screen(sock):
    msg_type = _recv_exact(sock, 4)
    if msg_type != MSG_SCR:
        raise ValueError(f'Expected SCR, got {msg_type}')
    length = struct.unpack('!I', _recv_exact(sock, 4))[0]
    return _recv_exact(sock, length)
