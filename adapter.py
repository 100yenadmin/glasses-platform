"""Hermes gateway transport for authenticated smart-glasses companion apps."""
import asyncio
from collections import deque
from dataclasses import dataclass, field
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


@dataclass(eq=False)
class Turn:
    """Reply state of one Hermes processing chain for a user. Hermes runs queued follow-up queries inside the
    opening query's processing, so one chain answers its queries one after another."""
    conn: Connection
    id: str  # the query being answered now
    reset: bool = False  # the device's own new_session: answers Hermes' /new confirm, ends with session_reset
    started: bool = False  # Hermes began processing the opener (on_processing_start)
    opener: str = ''  # the query that opened the chain; its completion ends the chain
    text: str = ''  # the reply so far: the latest stream segment or final send (replies are never chunked)
    stream: str | None = None  # message id of the live stream segment
    commentary: str = ''  # the latest interim commentary, the reply when Hermes delivered the answer that way
    ids: set = field(default_factory=set)

    def __post_init__(self):
        self.opener = self.opener or self.id
        self.ids.add(self.id)


class GlassesAdapter(BasePlatformAdapter):
    authorization_is_upstream = True
    supports_async_delivery = True
    MAX_MESSAGE_LENGTH = 1_000_000  # never chunk: a reply is one message, so the last one sent is the answer

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
        self._tokens, self._active_turn, self._user_locks = {}, {}, {}
        self._queries, self._delivered = {}, {}  # recent query id -> device; user -> recently delivered push ids
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
        return task

    def _lock(self, user):
        return self._user_locks.setdefault(user, asyncio.Lock())

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
            conn.flush_task = self._spawn(self._flush_later(conn, 2.0))
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
            self._release(conn)
        return ws

    def _release(self, conn):
        self.registry.remove(conn)
        self.broker.cancel_all(conn)
        if conn.flush_task:
            conn.flush_task.cancel()
        # Never settled: keep its pushes for the next connection, unless another device already received them.
        delivered = self._delivered.get(conn.identity.user, ())
        self.outbox.merge(conn.identity.user, [item for item in conn.pending if item[3] not in delivered])
        conn.pending = []

    async def _new_session(self, conn):
        conn.last_photo_at = 0
        await self._process_query(conn, {'type': 'query', 'text': '/new'}, reset=True)

    async def _process_query(self, conn, frame, reset=False):
        text = frame.get('text', '')
        text = text.strip() if isinstance(text, str) else ''
        if not text:
            await conn.send_json(protocol.error('Empty query.'))
            return
        user = conn.identity.user
        turn_id = frame.get('id')
        turn_id = turn_id if isinstance(turn_id, str) and turn_id else uuid4().hex
        self._queries[turn_id] = conn
        while len(self._queries) > 200:
            self._queries.pop(next(iter(self._queries)))
        # Hermes owns queued turns; on_processing_start selects their exact reply socket.
        self._active_turn.setdefault(user, Turn(conn, turn_id, reset=reset))
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
        event._glasses_reset = reset
        await self.handle_message(event)

    def _targets(self, user):
        # During a turn only the asking device gets its deltas, status and reply.
        turn = self._active_turn.get(user)
        live = self.registry.live(user)
        if turn:
            return [turn.conn] if turn.conn in live else []
        return live

    async def _flush_outbox(self, conn):
        user = conn.identity.user
        async with self._lock(user):
            if conn.flushed:
                return
            conn.flushed = True
            items = sorted({item[3]: item for item in [*self.outbox.drain(user), *conn.pending]}.values(),
                           key=lambda item: item[0])
            conn.pending, sent = [], 0
            try:
                for _, text, origin, message_id in items:
                    if not await conn.send_json(protocol.push(conn.protocol, text, origin, message_id)):
                        break
                    self._mark_delivered(user, message_id)
                    sent += 1
            finally:
                if sent < len(items):  # closed or cancelled: keep the rest queued, retry on the next frame
                    conn.flushed = False
                    self.outbox.merge(user, items[sent:])

    def _mark_delivered(self, user, message_id):
        self._delivered.setdefault(user, deque(maxlen=500)).append(message_id)

    async def _flush_later(self, conn, delay):
        await asyncio.sleep(delay)
        await self._flush_outbox(conn)

    async def _push(self, user, content, origin, message_id):
        # The per-user lock keeps queued and live pushes in order. Devices whose protocol is not settled
        # yet hold the push until their flush; with no device reachable it waits in the outbox.
        async with self._lock(user):
            reached = False
            for conn in self.registry.live(user):
                if not conn.flushed:
                    conn.pending.append((time.time(), content, origin, message_id))
                    reached = True
                elif await conn.send_json(protocol.push(conn.protocol, content, origin, message_id)):
                    self._mark_delivered(user, message_id)
                    reached = True
            if not reached:
                self.outbox.append(user, content, origin, message_id)
        return SendResult(success=True, message_id=message_id)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        user, metadata = str(chat_id), metadata or {}
        message_id = uuid4().hex
        # Human-decision prompts (text approvals, clarify questions) must reach every device in full.
        if metadata.get('is_approval_prompt') or metadata.get('glasses_prompt'):
            return await self._push(user, content, metadata.get('glasses_prompt') or 'approval', message_id)
        origin = metadata.get('origin') or ('cron' if metadata.get('job_id') else None)
        turn = self._active_turn.get(user)
        final = metadata.get('notify') and not metadata.get('_interim_send')
        if final and reply_to in self._queries and not (turn and reply_to in turn.ids):
            # A command answered inline while a turn runs (/stop, /status): the reply to that query alone.
            await self._deliver_reply(user, self._queries[reply_to], reply_to, content)
        elif turn is None or origin is not None:
            return await self._push(user, content, origin or 'message', message_id)
        elif metadata.get('expect_edits') and not metadata.get('_interim_send'):
            turn.text, turn.stream = content, message_id  # a new stream segment: the reply is the last one
            await self._reply_delta(user, turn)
        elif final:
            turn.text, turn.stream = content, None
        else:
            # Tool progress, commentary, busy and long-running notices: status detail only, never a reply.
            if metadata.get('_interim_send'):
                turn.commentary = content
            await self._status(user, 'working', content)
        return SendResult(success=True, message_id=message_id)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        user = str(chat_id)
        turn = self._active_turn.get(user)
        if turn and message_id == turn.stream:
            turn.text = content
            await self._reply_delta(user, turn)
        elif turn:
            await self._status(user, 'working', content)  # an edited progress bubble
        return SendResult(success=True, message_id=message_id)

    async def _reply_delta(self, user, turn):
        for conn in self._targets(user):
            if conn.protocol == 1:
                await conn.send_json(protocol.response_delta(turn.id, turn.text))

    async def _deliver_reply(self, user, conn, query_id, text):
        if not (conn in self.registry.live(user) and await conn.send_json(protocol.response(conn.protocol, text, query_id))):
            await self._push(user, text, 'reply', uuid4().hex)  # the asker went offline: deliver out of band

    async def _finish_query(self, user, turn):
        """Answer the query the chain is on, exactly once."""
        if turn.reset:  # v0 contract: new_session is answered by session_reset alone
            if turn.conn in self.registry.live(user):
                await turn.conn.send_json(protocol.session_reset())
        elif turn.text or turn.commentary:
            await self._deliver_reply(user, turn.conn, turn.id, turn.text or turn.commentary)
        await self._status(user, 'idle')

    async def send_slash_confirm(self, chat_id, title, message, session_key, confirm_id, metadata=None):
        # The device's new_session button is the user's explicit confirmation of the /new it sends; anything
        # else (a spoken "/new", /reload-mcp) falls back to Hermes' text prompt.
        turn = self._active_turn.get(str(chat_id))
        if not (turn and turn.reset):
            return SendResult(success=False, error='Not supported')
        from tools import slash_confirm
        await slash_confirm.resolve(session_key, confirm_id, 'once')
        return SendResult(success=True)

    async def send_clarify(self, *args, metadata=None, **kwargs):
        return await super().send_clarify(*args, metadata={**(metadata or {}), 'glasses_prompt': 'clarify'}, **kwargs)

    async def _status(self, user, state, detail=None):
        frame = protocol.status(state, detail)
        for conn in self._targets(str(user)):
            if conn.protocol != 1 or conn.last_status == frame:
                continue
            if state == 'thinking' and conn.last_status.get('state') == 'working':
                continue  # the typing refresh must not hide the latest activity detail
            conn.last_status = frame
            await conn.send_json(frame)

    async def send_typing(self, chat_id, metadata=None):
        await self._status(chat_id, 'thinking')

    async def stop_typing(self, chat_id):
        # Mid-turn typing pauses are not the end of a turn; on_processing_complete reports idle.
        if str(chat_id) not in self._active_turn:
            await self._status(chat_id, 'idle')

    async def on_processing_start(self, event):
        user = event.source.chat_id
        conn = getattr(event, '_glasses_connection', None) or self._queries.get(event.message_id)
        reset = getattr(event, '_glasses_reset', False)
        turn = self._active_turn.get(user)
        if conn and (turn is None or not turn.started):
            self._active_turn[user] = Turn(conn, event.message_id, reset=reset, started=True)
        elif conn and event.message_id != turn.id:
            # A queued follow-up runs inside the opener's processing: the previous query is answered first.
            await self._finish_query(user, turn)
            turn.conn, turn.id, turn.reset, turn.text, turn.stream, turn.commentary = (
                conn, event.message_id, reset, '', None, '')
            turn.ids.add(event.message_id)
        await self._status(user, 'thinking')

    async def on_processing_complete(self, event, outcome):
        user = event.source.chat_id
        turn = self._active_turn.get(user)
        # Follow-ups complete inside the opener's processing, before their reply is sent; only the opener's
        # completion (after Hermes' final send) ends the chain.
        if turn and turn.started and event.message_id == turn.opener:
            await self._finish_query(user, turn)
            self._active_turn.pop(user, None)

    async def get_chat_info(self, chat_id):
        return {'name': chat_id, 'type': 'dm'}

    async def capture_for_session(self, user, reason='tool'):
        async def capture():
            turn = self._active_turn.get(user)
            conn = self.registry.pick_camera(user, turn.conn if turn else None)
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
