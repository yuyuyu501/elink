import asyncio
from types import SimpleNamespace

from pyee.asyncio import AsyncIOEventEmitter

from elink.core import transport


def test_slow_negotiation_does_not_consume_live_control_heartbeat_deadline(tmp_path, monkeypatch):
    async def scenario():
        clock = [100.0]
        monkeypatch.setattr(transport, 'time', SimpleNamespace(monotonic=lambda: clock[0]))

        class Channel(AsyncIOEventEmitter):
            readyState = 'connecting'
            bufferedAmount = 0

            def send(self, raw):
                pass

        class Peer(AsyncIOEventEmitter):
            def __init__(self, config):
                super().__init__()
                self.channels = []
                self.localDescription = SimpleNamespace(sdp='offer', type='offer')

            def createDataChannel(self, *args, **kwargs):
                channel = Channel()
                self.channels.append(channel)
                return channel

            def addTransceiver(self, *args, **kwargs):
                pass

            def getTransceivers(self):
                return []

            async def createOffer(self):
                return self.localDescription

            async def setLocalDescription(self, description):
                # ICE gathering / HTTPS / DTLS may take longer than five seconds.
                clock[0] += 8

            async def setRemoteDescription(self, description):
                for channel in self.channels:
                    channel.readyState = 'open'
                    channel.emit('open')

        async def fingerprint(endpoint):
            return '0' * 64

        async def request(endpoint, fingerprint, method, path, **kwargs):
            if path == '/v1/info':
                return dict(protocol='elink', version=2, features=[])
            return dict(id='session', sdp='answer', type='answer')

        monkeypatch.setattr(transport, 'RTCPeerConnection', Peer)
        monkeypatch.setattr(transport, 'fingerprint', fingerprint)
        monkeypatch.setattr(transport, 'request', request)
        monkeypatch.setattr(transport, 'read_text', lambda: None)
        client = transport.Client(tmp_path, play_audio=False)
        # Drive the heartbeat explicitly, without starting unrelated watchers.
        client.task = lambda coroutine: coroutine.close()
        await client.connect('127.0.0.1:50000', dict(audio=False))
        disconnected = []
        client.schedule_disconnect = lambda: disconnected.append(True)

        async def one_tick(delay):
            raise asyncio.CancelledError

        with monkeypatch.context() as tick:
            tick.setattr(asyncio, 'sleep', one_tick)
            try:
                await client.heartbeat()
            except asyncio.CancelledError:
                pass
        assert not disconnected, 'a newly opened channel must get a full heartbeat response interval'

        # Keep the existing liveness timeout after the channel opens.
        clock[0] += 5.1
        await client.heartbeat()
        assert disconnected == [True]

        old_channel = client.control
        client.pc = object()
        last_pong = client.last_pong
        old_channel.emit('open')
        assert client.last_pong == last_pong, 'an old session must not reset the current deadline'

    asyncio.run(scenario())
