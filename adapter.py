"""Hermes gateway transport for authenticated smart-glasses companion apps."""
import asyncio
import logging
import time
from uuid import uuid4

from aiohttp import web, WSMsgType
from gateway.config import HomeChannel, Platform
from gateway.platforms._shared import extra_or_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult, cache_image_from_bytes_async
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.shared_ingress import bind_listener

from . import protocol
from .capture import PhotoBroker, decode_photo, should_capture_photo
from .devices import Connection, Outbox, Registry, authenticate, parse_devices

logger = logging.getLogger(__name__)
ACTIVE = None


class GlassesAdapter(BasePlatformAdapter):
    authorization_is_upstream = True
    supports_async_delivery = True
    MAX_MESSAGE_LENGTH = 4000
    REQUIRES_EDIT_FINALIZE = True

    def __init__(self, config):
        super().__init__(config, Platform('glasses'))
        def setting(key, default=''):
            return extra_or_secret(config.extra, key, 'GLASSES_' + key.upper(), default)
        self._device_spec, self._single_token = setting('devices'), setting('token')
        self._host, self._port, self._path = str(setting('host', '0.0.0.0')), int(setting('port', 8765)), str(setting('path', '/voice'))
        self._home_channel = str(setting('home_channel'))
        self._auto_capture = setting('auto_capture', 'keywords')
        if self._auto_capture is False:
            self._auto_capture = 'off'
        if self._auto_capture not in ('off', 'keywords'):
            raise ValueError('auto_capture must be keywords or off')
        self._photo_timeout = float(setting('photo_timeout', 25))
        self._max_frame_mb = float(setting('max_frame_mb', 8))
        self.registry = Registry()
        self.outbox = Outbox(int(setting('outbox_max', 50)), float(setting('outbox_ttl_hours', 24)))
        self.broker = PhotoBroker(self._max_frame_mb)
        self._tokens, self._active_turn = {}, {}
        self._runner, self._app, self._loop = None, None, None

    async def connect(self, *, is_reconnect=False):
        global ACTIVE
        self._tokens = parse_devices(self._device_spec, self._single_token)
        if not self._tokens:
            logger.error('glasses: no device tokens configured; refusing to start')
            return False
        if not self._home_channel:
            self._home_channel = next(iter(self._tokens.values())).user
        if self.config.home_channel is None:
            self.config.home_channel = HomeChannel(self.platform, self._home_channel, self._home_channel)
        self._loop = asyncio.get_running_loop()
        self._app = web.Application(client_max_size=int(self._max_frame_mb * 1024 * 1024))
        self._app.router.add_get(self._path, self._websocket)
        self._app.router.add_get('/health', self._health)
        # Query credentials must never reach aiohttp's access log.
        self._runner = await bind_listener(self, self._app, self._host, self._port, self._path, access_log=None)
        self._mark_connected()
        self._wire_plugin_handlers(None)
        ACTIVE = self
        return True

    async def _health(self, request):
        return web.json_response({'ok': True})

    async def disconnect(self):
        global ACTIVE
        for user in list(self.registry.by_user):
            for conn in self.registry.live(user):
                self.broker.cancel_all(conn)
                await conn.ws.close(code=1001, message=b'shutting down')
        await self.cancel_background_tasks()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._active_turn.clear()
        self._mark_disconnected()
        if ACTIVE is self:
            ACTIVE = None

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        def done(task):
            self._background_tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                logger.error('glasses: query processing failed (%s)', type(task.exception()).__name__)
        task.add_done_callback(done)

    async def _websocket(self, request):
        auth = request.headers.get('Authorization', '')
        token = auth[7:] if auth.lower().startswith('bearer ') else request.query.get('token')
        identity = authenticate(token, self._tokens)
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=int(self._max_frame_mb * 1024 * 1024))
        await ws.prepare(request)
        if identity is None:
            await ws.close(code=4401, message=b'unauthorized')
            return ws
        conn = Connection(ws, identity)
        await self.registry.add(conn)
        try:
            await conn.send_json(protocol.welcome())
            # Queued pushes wait until the protocol is known: after `hello` (v1), before the first other frame,
            # or after a short grace period (v0 clients that only listen).
            self._spawn(self._flush_later(conn, 2.0))
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                frame = protocol.parse_client_frame(msg.data)
                if frame is None:
                    logger.debug('glasses: ignored malformed frame')
                    continue
                kind = frame['type']
                if kind != 'hello':
                    await self._flush_outbox(conn)
                if kind == 'hello':
                    conn.protocol = 1
                    device = frame.get('device')
                    capabilities = device.get('capabilities', {}) if isinstance(device, dict) else {}
                    conn.capabilities = capabilities if isinstance(capabilities, dict) else {}
                    await conn.send_json(protocol.hello_ack(identity.user, identity.device))
                    await self._flush_outbox(conn)
                elif kind == 'query':
                    self._spawn(self._process_query(conn, frame))
                elif kind in ('photo', 'photo_error'):
                    self.broker.resolve(conn, frame)
                elif kind == 'new_session':
                    self._spawn(self._new_session(conn))
                elif kind == 'ping':
                    await conn.send_json(protocol.pong())
                elif kind == 'debug':
                    logger.info('glasses device debug: %s', str(frame.get('msg', ''))[:500])
        finally:
            self.registry.remove(conn)
            self.broker.cancel_all(conn)
        return ws

    async def _new_session(self, conn):
        conn.last_photo_at = 0
        await self._process_query(conn, {'type': 'query', 'text': '/new'})
        await conn.send_json(protocol.session_reset())

    async def _process_query(self, conn, frame):
        text = frame.get('text', '')
        text = text.strip() if isinstance(text, str) else ''
        if not text:
            await conn.send_json(protocol.error('Empty query.'))
            return
        user = conn.identity.user
        turn_id = frame.get('id')
        turn_id = turn_id if isinstance(turn_id, str) and turn_id else uuid4().hex
        # Hermes owns queued turns; on_processing_start selects their exact reply socket.
        self._active_turn.setdefault(user, (conn, turn_id))
        photo = None
        image = frame.get('image') if conn.protocol == 1 else None
        if isinstance(image, dict):
            photo = decode_photo(image.get('data'), self.broker.max_bytes)
        elif self._auto_capture == 'keywords' and should_capture_photo(text, conn.last_photo_at, time.monotonic()):
            device = self.registry.pick_camera(user, conn)
            if device:
                photo = await self.broker.request(device, 'keyword', self._photo_timeout)
                if photo is None:
                    text = '(No photo could be captured from the glasses.) ' + text
        path = None
        if photo:
            try:
                path = await cache_image_from_bytes_async(photo)
            except ValueError:
                photo = None
                text = '(No photo could be captured from the glasses.) ' + text
        if photo:
            conn.last_photo_at = time.monotonic()
        raw = {k: v for k, v in frame.items() if k not in ('image', 'data')}
        event = MessageEvent(
            text=text, message_type=MessageType.PHOTO if path else MessageType.TEXT,
            source=self.build_source(chat_id=user, chat_name=user, chat_type='dm', user_id=user, user_name=user),
            user_id=user, user_name=user, message_id=turn_id,
            media_urls=[path] if path else [], media_types=['image/jpeg'] if path else [], raw_message=raw)
        event._glasses_connection = conn  # retained through Hermes startup replay
        await self.handle_message(event)

    def _targets(self, user):
        active = self._active_turn.get(user)
        live = self.registry.live(user)
        if active and active[0] in live:
            return [active[0]]
        return live

    async def _flush_outbox(self, conn):
        if conn.flushed:
            return
        conn.flushed = True
        for _, text, origin, message_id in self.outbox.drain(conn.identity.user):
            await conn.send_json(protocol.push(conn.protocol, text, origin, message_id))

    async def _flush_later(self, conn, delay):
        await asyncio.sleep(delay)
        await self._flush_outbox(conn)

    async def _push(self, user, content, origin, message_id):
        # Only devices whose protocol is settled get live pushes; otherwise queue (the next flush delivers it).
        ready = [c for c in self.registry.live(user) if c.flushed]
        for conn in ready:
            await conn.send_json(protocol.push(conn.protocol, content, origin, message_id))
        if not ready:
            self.outbox.append(user, content, origin, message_id)
        return SendResult(success=True, message_id=message_id)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        user, metadata = str(chat_id), metadata or {}
        message_id = uuid4().hex
        # Text approval prompts are interim-tagged by Hermes but must reach every client in full.
        if metadata.get('is_approval_prompt'):
            return await self._push(user, content, 'approval', message_id)
        origin = metadata.get('origin') or ('cron' if metadata.get('job_id') else None)
        active = self._active_turn.get(user)
        if active and origin is None:
            final = bool(metadata.get('notify')) and not metadata.get('_interim_send')
            targets = self._targets(user)
            if not targets and final:
                self.outbox.append(user, content, 'message', message_id)  # asker went offline mid-turn
            for conn in targets:
                if final:
                    await conn.send_json(protocol.response(conn.protocol, content, active[1]))
                elif conn.protocol == 1:
                    await conn.send_json(protocol.response_delta(active[1], content))
            return SendResult(success=True, message_id=message_id)
        return await self._push(user, content, origin or 'message', message_id)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        active = self._active_turn.get(str(chat_id))
        if active:
            targets = self._targets(str(chat_id))
            if not targets and finalize:
                self.outbox.append(str(chat_id), content, 'message', uuid4().hex)
            for conn in targets:
                if finalize:
                    await conn.send_json(protocol.response(conn.protocol, content, active[1]))
                elif conn.protocol == 1:
                    await conn.send_json(protocol.response_delta(active[1], content))
        return SendResult(success=True, message_id=message_id)

    async def _status(self, user, state):
        for conn in self._targets(str(user)):
            if conn.protocol == 1:
                await conn.send_json(protocol.status(state))

    async def send_typing(self, chat_id, metadata=None):
        await self._status(chat_id, 'thinking')

    async def stop_typing(self, chat_id):
        await self._status(chat_id, 'idle')

    async def on_processing_start(self, event):
        user = event.source.chat_id
        conn = getattr(event, '_glasses_connection', None)
        if conn:
            self._active_turn[user] = (conn, event.message_id)
        await self._status(user, 'thinking')

    async def on_processing_complete(self, event, outcome):
        user = event.source.chat_id
        active = self._active_turn.get(user)
        if active and active[1] == event.message_id:
            await self._status(user, 'idle')
            self._active_turn.pop(user, None)

    async def get_chat_info(self, chat_id):
        return {'name': chat_id, 'type': 'dm'}

    async def capture_for_session(self, user, reason='tool'):
        async def capture():
            active = self._active_turn.get(user)
            conn = self.registry.pick_camera(user, active[0] if active else None)
            if conn is None:
                return None
            data = await self.broker.request(conn, reason, self._photo_timeout)
            if data is None:
                return None
            conn.last_photo_at = time.monotonic()
            try:
                return data, await cache_image_from_bytes_async(data)
            except ValueError:
                return None
        if asyncio.get_running_loop() is self._loop:
            return await capture()
        future = asyncio.run_coroutine_threadsafe(capture(), self._loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), self._photo_timeout + 5)
        except TimeoutError:
            future.cancel()
            return None
