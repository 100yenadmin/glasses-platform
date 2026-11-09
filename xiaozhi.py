"""Stock xiaozhi OTA discovery and WebSocket translation into the glasses v1 transport."""
import asyncio
import base64
import binascii
import json
import logging
import struct
import time
from contextlib import suppress
from dataclasses import dataclass, field
from uuid import uuid4

from aiohttp import web, WSMsgType
from gateway.platforms._shared import extra_or_secret

from .devices import Connection, authenticate
from .ogg import mux_opus, demux_opus

logger = logging.getLogger(__name__)
HELLO_TIMEOUT = 10
AUDIO_TIMEOUT = 20
_missing_ffmpeg = False


def binary_packet(packet, version, timestamp=0):
    if version == 2:
        return struct.pack('!HHIII', 2, 0, 0, timestamp, len(packet)) + packet
    if version == 3:
        return struct.pack('!BBH', 0, 0, len(packet)) + packet
    return packet


def strip_binary(data, version):
    if version == 2:
        if len(data) < 16:
            return None
        v, kind, _, _, size = struct.unpack_from('!HHIII', data)
        if v != 2 or kind != 0 or size != len(data) - 16:
            return None
        return data[16:]
    if version == 3:
        if len(data) < 4:
            return None
        kind, _, size = struct.unpack_from('!BBH', data)
        if kind != 0 or size != len(data) - 4:
            return None
        return data[4:]
    return data


async def transcode(data):
    """MP3 (or WAV for the simulator) to fixed-duration 24 kHz mono Opus, without shell or files."""
    global _missing_ffmpeg
    try:
        process = await asyncio.create_subprocess_exec(
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0', '-vn',
            '-ac', '1', '-ar', '24000', '-c:a', 'libopus', '-frame_duration', '60',
            '-f', 'ogg', 'pipe:1', stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except FileNotFoundError:
        if not _missing_ffmpeg:
            logger.warning('xiaozhi: ffmpeg unavailable; replies will show text only')
            _missing_ffmpeg = True
        return []
    try:
        output, _ = await asyncio.wait_for(process.communicate(data), 20)
        return demux_opus(output) if process.returncode == 0 else []
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


@dataclass
class Speech:
    text: str
    ref: str | None
    audio: asyncio.Future
    push: tuple | None = None
    at: float = field(default_factory=time.monotonic)


class XiaozhiSocket:
    """Accept adapter frames promptly; serialize speech while audio can arrive independently."""
    def __init__(self, ws, session_id, version=1, audio_params=None, *, audio_timeout=None, pace=0.055):
        self.ws, self.session_id, self.version = ws, session_id, version
        self.audio_params = audio_params or {}
        self.audio_timeout = AUDIO_TIMEOUT if audio_timeout is None else audio_timeout
        self.pace = pace
        self.queue = asyncio.Queue()
        self.waiting, self.unsent = {}, {}
        self.worker = None
        self.thinking = False
        self.speaking = False

    @property
    def closed(self):
        return self.ws.closed

    async def _json(self, kind, **fields):
        await self.ws.send_json({'type': kind, 'session_id': self.session_id, **fields})

    async def send_json(self, frame):
        if self.closed:
            return False
        kind = frame.get('type')
        if kind == 'transcript':
            self.thinking = False
            await self._json('stt', text=frame.get('text', ''))
        elif kind == 'status':
            if frame.get('state') in ('thinking', 'working') and not self.thinking:
                self.thinking = True
                await self._json('llm', emotion='thinking', text='🤔')
            elif frame.get('state') == 'idle':
                self.thinking = False
        elif kind in ('response', 'push', 'error'):
            if kind != 'push':
                self.thinking = False
            ref = frame.get('turn') if kind == 'response' else frame.get('id') if kind == 'push' else None
            future = asyncio.get_running_loop().create_future()
            push = (time.time(), frame.get('text', ''), frame.get('origin', 'message'), ref) if kind == 'push' else None
            item = Speech(frame.get('text', frame.get('message', '')), ref, future, push)
            if ref:
                self.waiting[ref] = item
            else:
                future.set_result(None)
            if push:
                self.unsent[ref] = push
            self.queue.put_nowait(item)
            if self.worker is None or self.worker.done():
                self.worker = asyncio.create_task(self._speak())
        elif kind == 'audio' and frame.get('mime') == 'audio/mpeg':
            item = self.waiting.get(frame.get('ref'))
            if item and not item.audio.done():
                item.audio.set_result(frame.get('data'))
        elif kind == 'display':
            await self._json('alert', status='Hermes', message=frame.get('text', ''), emotion='neutral')
        # Other adapter frames have no xiaozhi equivalent.
        return True

    async def _speak(self):
        try:
            while not self.queue.empty():
                item = await self.queue.get()
                try:
                    remaining = max(0, self.audio_timeout - (time.monotonic() - item.at))
                    if item.audio.done():
                        data = item.audio.result()
                    else:
                        data = await asyncio.wait_for(asyncio.shield(item.audio), remaining)
                except TimeoutError:
                    data = None
                finally:
                    self.waiting.pop(item.ref, None)
                packets = []
                if data:
                    try:
                        packets = await transcode(base64.b64decode(data, validate=True))
                    except (ValueError, binascii.Error, OSError, TimeoutError):
                        logger.debug('xiaozhi: audio conversion failed; showing text only')
                await self._json('tts', state='start')
                self.speaking = True
                await self._json('tts', state='sentence_start', text=item.text)
                for index, packet in enumerate(packets):
                    await self.ws.send_bytes(binary_packet(packet, self.version, index * 60))
                    await asyncio.sleep(self.pace)
                await self._json('tts', state='stop')
                self.speaking = False
                if item.push:
                    self.unsent.pop(item.ref, None)
        except (ConnectionError, RuntimeError, OSError):
            logger.debug('xiaozhi: speech socket closed')

    async def abort(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        self.waiting.clear()
        while not self.queue.empty():
            self.queue.get_nowait()
        if self.speaking and not self.closed:
            await self._json('tts', state='stop')
        self.speaking = False

    async def close(self, **kwargs):
        await self.ws.close(**kwargs)
        await self.abort()


class Recording:
    def __init__(self, params, mode='manual', max_bytes=8 * 1024 * 1024):
        self.rate = params.get('sample_rate', 16000)
        self.channels = params.get('channels', 1)
        self.frame_ms = params.get('frame_duration', 60)
        if self.rate not in (8000, 12000, 16000, 24000, 48000) or self.channels not in (1, 2):
            raise ValueError('Unsupported Opus parameters')
        if self.frame_ms not in (2.5, 5, 10, 20, 40, 60, 80, 100, 120):
            raise ValueError('Unsupported Opus frame duration')
        self.mode, self.max_bytes = mode, max_bytes
        self.packets, self.speech, self.quiet, self.size = [], False, 0, 0
        self.deadline = time.monotonic() + 15

    def add(self, packet):
        self.size += len(packet)
        if self.size > self.max_bytes:
            return True
        self.packets.append(packet)
        self.speech |= len(packet) > 40
        self.quiet = self.quiet + self.frame_ms if len(packet) <= 12 and self.speech else 0
        return (self.mode in ('auto', 'realtime') and self.quiet >= 800
                or len(self.packets) * self.frame_ms >= 15000)

    def query(self):
        if not self.speech:
            return None
        data = mux_opus(self.packets, self.rate, self.channels, self.frame_ms)
        return {'type': 'query', 'id': uuid4().hex, 'text': '',
                'audio': {'data': base64.b64encode(data).decode(), 'mime': 'audio/ogg'}}


def register_routes(adapter, app):
    async def ota(request):
        token = request.match_info['token']
        if authenticate(token, adapter._tokens) is None:
            raise web.HTTPUnauthorized()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest() from None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest()
        application = body.get('application') or body.get('firmware') or {}
        version = application.get('version', body.get('version', '0')) if isinstance(application, dict) else '0'
        scheme = 'wss' if request.headers.get('X-Forwarded-Proto', request.scheme).lower() == 'https' else 'ws'
        url = extra_or_secret(adapter.config.extra, 'xiaozhi_url', 'GLASSES_XIAOZHI_URL', '')
        return web.json_response({'websocket': {'url': str(url or f'{scheme}://{request.host}/xiaozhi'),
                                               'token': token, 'version': 1},
                                  'server_time': {'timestamp': int(time.time() * 1000), 'timezone_offset': 0},
                                  'firmware': {'version': str(version), 'url': ''}})

    async def websocket(request):
        auth = request.headers.get('Authorization', '')
        token = auth[7:] if auth.lower().startswith('bearer ') else request.query.get('token')
        identity = authenticate(token, adapter._tokens)
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=int(adapter._max_frame_mb * 1024 * 1024))
        await ws.prepare(request)
        if identity is None:
            await ws.close(code=4401, message=b'unauthorized')
            return ws
        try:
            hello = await ws.receive_json(timeout=HELLO_TIMEOUT)
            if not isinstance(hello, dict) or hello.get('type') != 'hello':
                raise ValueError('Expected hello')
            version = int(request.headers.get('Protocol-Version', hello.get('version', 1)))
            params = hello.get('audio_params') or {}
            if version not in (1, 2, 3) or not isinstance(params, dict):
                raise ValueError('Unsupported hello')
            Recording(params)  # validate before registration
        except (ValueError, TypeError, TimeoutError):
            await ws.close(code=4400, message=b'invalid hello')
            return ws
        socket = XiaozhiSocket(ws, uuid4().hex, version, params)
        conn = Connection(socket, identity, protocol=1,
                          capabilities={'audio_out': ['audio/mpeg'], 'speaker': True, 'mic': True, 'display': True})
        recording = None

        def submit(frame):
            if frame:
                conn.asking[frame['id']] = time.monotonic()
                adapter._spawn(adapter._process_query(conn, frame))

        try:
            await ws.send_json({'type': 'hello', 'transport': 'websocket', 'session_id': socket.session_id,
                                'audio_params': {'format': 'opus', 'sample_rate': 24000,
                                                 'channels': 1, 'frame_duration': 60}})
            await adapter.registry.add(conn)
            await adapter._flush_outbox(conn)
            while not ws.closed:
                timeout = max(0.001, recording.deadline - time.monotonic()) if recording else None
                try:
                    msg = await ws.receive(timeout=timeout)
                except TimeoutError:
                    submit(recording.query())
                    recording = None
                    continue
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
                if msg.type == WSMsgType.BINARY and recording:
                    packet = strip_binary(msg.data, version)
                    if packet is not None and recording.add(packet):
                        submit(recording.query())
                        recording = None
                elif msg.type == WSMsgType.TEXT:
                    try:
                        frame = json.loads(msg.data)
                    except ValueError:
                        continue
                    if not isinstance(frame, dict):
                        continue
                    kind = frame.get('type')
                    if kind == 'listen':
                        state = frame.get('state')
                        if state == 'start':
                            recording = Recording(params, frame.get('mode', 'manual'),
                                                  int(adapter._max_frame_mb * 1024 * 1024))
                        elif state == 'stop' and recording:
                            submit(recording.query())
                            recording = None
                    elif kind == 'abort':
                        recording = None
                        await socket.abort()
                        turn = adapter._active_turn.get(identity.user)
                        if turn and turn.conn is conn and turn.id in conn.asking:
                            submit({'type': 'query', 'id': uuid4().hex, 'text': '/stop'})
                    else:
                        logger.debug('xiaozhi: ignored device frame type')
        finally:
            try:
                await socket.close()
            finally:
                adapter._release(conn)
                # Synchronous, like _release: preserve pushes even when socket shutdown is cancelled.
                adapter.outbox.merge(identity.user, list(socket.unsent.values()))
        return ws

    app.router.add_post('/xiaozhi/ota/{token}', ota)
    app.router.add_get('/xiaozhi', websocket)
