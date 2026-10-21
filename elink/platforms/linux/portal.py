"""Wayland RemoteDesktop/ScreenCast Portal and PipeWire capture.

Uses the Portal Notify input family exclusively (never mixes it with EIS).
The compositor owns the screen-selection and permission dialog.
"""
import asyncio
import concurrent.futures
import os
import threading
import uuid

import numpy as np


DESTINATION = 'org.freedesktop.portal.Desktop'
PATH = '/org/freedesktop/portal/desktop'
REMOTE = 'org.freedesktop.portal.RemoteDesktop'
SCREEN = 'org.freedesktop.portal.ScreenCast'
_lock = threading.Lock()
_current = None


class PortalSession:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name='elink-portal', daemon=True)
        self.thread.start()
        self.bus = None
        self.path = None
        self.fd = None
        self.stream = None
        self.size = None
        self.references = 0
        self.pending = None

    async def call(self, interface, member, signature='', body=None, path=PATH):
        from dbus_next import Message, MessageType
        response = await self.bus.call(Message(destination=DESTINATION, path=path,
                                               interface=interface, member=member,
                                               signature=signature, body=body or []))
        if response.message_type == MessageType.ERROR:
            raise RuntimeError(f'Portal {member}: {response.error_name}: {response.body}')
        return response

    async def request(self, interface, member, signature, body, options):
        from dbus_next import Variant
        token = 'elink' + uuid.uuid4().hex
        options['handle_token'] = Variant('s', token)
        sender = self.bus.unique_name[1:].replace('.', '_')
        expected = f'/org/freedesktop/portal/desktop/request/{sender}/{token}'
        future = asyncio.get_running_loop().create_future()

        def receive(message):
            if message.path == expected and message.interface == 'org.freedesktop.portal.Request' and message.member == 'Response':
                if not future.done():
                    future.set_result(message.body)

        rule = f"type='signal',interface='org.freedesktop.portal.Request',path='{expected}'"
        from dbus_next import Message
        await self.bus.call(Message(destination='org.freedesktop.DBus', path='/org/freedesktop/DBus',
                                    interface='org.freedesktop.DBus', member='AddMatch', signature='s', body=[rule]))
        self.bus.add_message_handler(receive)
        try:
            await self.call(interface, member, signature, [*body, options])
            response, result = await asyncio.wait_for(future, 120)
            if response:
                raise RuntimeError(f'Portal permission was cancelled or denied ({response})')
            return {key: value.value for key, value in result.items()}
        finally:
            self.bus.remove_message_handler(receive)
            await self.bus.call(Message(destination='org.freedesktop.DBus', path='/org/freedesktop/DBus',
                                        interface='org.freedesktop.DBus', member='RemoveMatch', signature='s', body=[rule]))

    async def open(self):
        from dbus_next import Variant
        from dbus_next.aio import MessageBus
        self.bus = await MessageBus(negotiate_unix_fd=True).connect()
        result = await self.request(REMOTE, 'CreateSession', 'a{sv}', [],
                                    {'session_handle_token': Variant('s', 'elink' + uuid.uuid4().hex)})
        self.path = result['session_handle']
        await self.request(REMOTE, 'SelectDevices', 'oa{sv}', [self.path], {'types': Variant('u', 3)})
        await self.request(SCREEN, 'SelectSources', 'oa{sv}', [self.path],
                           {'types': Variant('u', 1), 'multiple': Variant('b', False), 'cursor_mode': Variant('u', 2)})
        result = await self.request(REMOTE, 'Start', 'osa{sv}', [self.path, ''], {})
        if result.get('devices', 0) & 3 != 3 or not result.get('streams'):
            raise RuntimeError('Portal did not grant screen, keyboard and pointer access')
        self.stream, properties = result['streams'][0]
        self.size = properties['size'].value
        response = await self.call(SCREEN, 'OpenPipeWireRemote', 'oa{sv}', [self.path, {}])
        self.fd = os.dup(response.unix_fds[response.body[0]])
        for descriptor in response.unix_fds:
            os.close(descriptor)

    def ensure_open(self):
        with _lock:
            if self.pending is None:
                self.pending = asyncio.run_coroutine_threadsafe(self.open(), self.loop)
            pending = self.pending
        try:
            pending.result(timeout=125)
        except concurrent.futures.TimeoutError as exc:
            pending.cancel()
            raise RuntimeError('Portal permission request timed out') from exc

    def notify(self, member, signature, values):
        if not self.pending or not self.pending.done() or self.pending.exception():
            raise RuntimeError('Screen sharing permission has not been granted')
        asyncio.run_coroutine_threadsafe(self.call(REMOTE, member, 'oa{sv}' + signature,
                                                   [self.path, {}, *values]), self.loop).result(timeout=2)

    def close(self):
        async def close():
            try:
                pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if self.path:
                    await self.call('org.freedesktop.portal.Session', 'Close', path=self.path)
            finally:
                if self.bus:
                    self.bus.disconnect()
        try:
            asyncio.run_coroutine_threadsafe(close(), self.loop).result(timeout=3)
        finally:
            if self.fd is not None:
                os.close(self.fd)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=3)


def acquire():
    global _current
    with _lock:
        if _current is None:
            _current = PortalSession()
        _current.references += 1
        return _current


def release(session):
    global _current
    with _lock:
        session.references -= 1
        close = session.references == 0
        if close:
            _current = None
    if close:
        session.close()


class PortalCapture:
    name = 'portal-pipewire'
    cursor_visible = None

    def __init__(self):
        self.session = acquire()
        self.pipeline = None
        try:
            import gi
            gi.require_version('Gst', '1.0')
            from gi.repository import Gst
            self.Gst = Gst
            Gst.init(None)
            self.session.ensure_open()
            self.pipeline = Gst.parse_launch(
                f'pipewiresrc fd={self.session.fd} path={self.session.stream} do-timestamp=true ! '
                'videoconvert ! video/x-raw,format=BGRx ! appsink name=frames sync=false drop=true max-buffers=1')
            self.sink = self.pipeline.get_by_name('frames')
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError('PipeWire capture failed to start')
        except BaseException:
            self.close()
            raise

    def read(self, draw_mouse=True):
        if not draw_mouse:
            raise RuntimeError('This Wayland backend embeds the remote cursor; use remote mouse mode')
        sample = self.sink.emit('try-pull-sample', self.Gst.SECOND)
        if sample is None:
            raise RuntimeError('PipeWire frame timed out or screen-sharing permission was revoked')
        structure = sample.get_caps().get_structure(0)
        width, height = structure.get_value('width'), structure.get_value('height')
        buffer = sample.get_buffer()
        ok, mapping = buffer.map(self.Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError('Cannot map PipeWire frame')
        try:
            stride = len(mapping.data) // height
            return np.frombuffer(mapping.data, dtype=np.uint8).reshape(height, stride)[:, :width * 4].reshape(height, width, 4).copy()
        finally:
            buffer.unmap(mapping)

    def close(self):
        if self.pipeline:
            self.pipeline.set_state(self.Gst.State.NULL)
            self.pipeline = None
        if self.session:
            release(self.session)
            self.session = None


class PortalInput:
    def __init__(self, pad_backend, notify):
        self.session = acquire()
        self.pad_backend, self.pads = pad_backend, None

    def emit(self, event):
        from Xlib import XK
        from .keyboard import keysym_name
        kind = event['type']
        if kind == 'pad':
            if self.pad_backend == 'disabled':
                raise RuntimeError('Virtual gamepads are disabled')
            if self.pads is None:
                from .gamepad import UInputPads
                self.pads = UInputPads()
            self.pads.update(event['index'], event['values'])
        elif kind == 'key':
            name = keysym_name(event)
            key = XK.string_to_keysym(name) if name else 0
            if not key:
                raise RuntimeError('Unknown key mapping')
            self.session.notify('NotifyKeyboardKeysym', 'iu', [key, int(event['down'])])
        elif kind == 'button':
            self.session.notify('NotifyPointerButton', 'iu',
                                [{'left': 0x110, 'right': 0x111, 'middle': 0x112}[event['button']], int(event['down'])])
        elif kind == 'wheel':
            self.session.notify('NotifyPointerAxis', 'dd', [0.0, -event['delta'] / 120 * 15.0])
        elif kind == 'move':
            if event['absolute']:
                width, height = self.session.size or (1, 1)
                self.session.notify('NotifyPointerMotionAbsolute', 'udd',
                                    [self.session.stream, event['x'] * (width - 1) / 65535, event['y'] * (height - 1) / 65535])
            else:
                self.session.notify('NotifyPointerMotion', 'dd', [float(event['x']), float(event['y'])])

    def close(self):
        if self.pads:
            self.pads.close()
        if self.session:
            release(self.session)
            self.session = None
