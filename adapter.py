"""Hermes gateway transport for authenticated smart-glasses companion apps."""
import asyncio
import base64
from collections import deque, OrderedDict
from dataclasses import dataclass, field
import logging
import json
import re
import time
from uuid import uuid4

from aiohttp import web, WSMsgType
from gateway.config import HomeChannel, Platform
from gateway.platforms._shared import extra_or_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult, cache_image_from_bytes_async
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome

from . import audio, protocol
from .capture import PhotoBroker, decode_photo, should_capture_photo
from .devices import Connection, Outbox, Registry, authenticate, parse_devices
from .http_api import register_routes
from .omi import register_routes as register_omi_routes
from .xiaozhi import register_routes as register_xiaozhi_routes

logger = logging.getLogger(__name__)
ACTIVE = None
# Prompts a turn waits on: they reach the device at once, even while it waits for an answer.
PROMPT_ORIGINS = ('approval', 'clarify')


# Spoken answers to an approval prompt: a voice user cannot type /approve. Only these exact phrases count; anything
# else goes to the agent as usual. "Yes" approves this one command once, never for the session or always.
_YES = {'yes', 'yeah', 'yep', 'yes please', 'approve', 'approved', 'approve it', 'allow', 'allow it', 'go ahead',
        'do it', 'yes do it', 'run it', 'ok', 'okay', 'sure'}
_NO = {'no', 'nope', 'deny', 'denied', 'deny it', 'block it', 'dont', 'do not', 'dont do it', 'no dont',
       'stop', 'cancel', 'no thanks'}


def spoken_choice(text):
    """'once' or 'deny' when the whole utterance is a yes or a no, else None."""
    words = re.sub(r"[^a-z ]+", '', str(text or '').lower().replace('\u2019', "'").replace("'", '')).split()
    if words[:2] == ['hey', 'hermes']:
        words = words[2:]
    elif words[:1] == ['hermes']:
        words = words[1:]
    phrase = ' '.join(words)
    return 'once' if phrase in _YES else 'deny' if phrase in _NO else None


def hermes_approval_words():
    """Hermes' own bare-word approval replies, word -> 'once', 'deny', or None (session/always). While an approval
    blocks, Hermes takes these exact words (stripped, lower-cased) as /approve or /deny for its OLDEST request."""
    words = {'\U0001f44d': 'once', '\U0001f44e': 'deny'}
    try:
        from gateway.run_busy import approval_input_words
        lists = [(approval_input_words(key), choice) for key, choice in
                 (('approve', 'once'), ('deny', 'deny'), ('always', None), ('session', None))]
    except Exception:  # pragma: no cover - the English list of Hermes 0.22.2 and 0.23.0
        lists = [('approve yes ok okay confirm y'.split(), 'once'), ('deny no reject cancel n'.split(), 'deny'),
                 (['always', 'approve always', 'always approve'], None),
                 (['session', 'approve session', 'session approve'], None)]
    for group, choice in lists:
        for word in group:
            words.setdefault(word, choice)
    return words


@dataclass
class Approval:
    session_key: str
    request_id: str | None
    choices: list
    at: float
    shown: bool = False  # its question has gone out to the devices (or the outbox)
    connections: set = field(default_factory=set)  # devices that received the button prompt


def _outbox_path():
    """The profile's own file for pushes waiting for an offline device."""
    from hermes_constants import get_hermes_home
    return get_hermes_home() / 'glasses' / 'outbox.json'


def _as_media(metadata):
    return {**(metadata or {}), 'glasses_media': True}


# Hermes wraps cron deliveries in a header (job name and id) and a footer (how to stop the job). Read aloud on
# glasses that is noise: the wearer hears the job's own words, and the push frame says it came from cron.
_CRON_WRAPPER = re.compile(
    r'\ACronjob Response: (?P<name>[^\n]*)\n\(job_id: [^)\n]*\)\n-+\n\n(?:(?P<body>.*)\n\n)?'
    r'To stop or manage this job, send me a new message [^\n]*\Z', re.S)


def _unwrap_cron(content):
    match = _CRON_WRAPPER.match(content) if isinstance(content, str) else None
    if not match:
        return content
    return (match.group('body') or '').strip() or match.group('name').strip() or content


@dataclass(eq=False)
class Turn:
    """Reply state of one Hermes processing chain for a user. Hermes runs queued follow-up queries inside the
    opening query's processing, so one chain answers its queries one after another."""
    conn: Connection
    id: str  # the query being answered now
    reset: bool = False  # the device's own new_session: answers Hermes' /new confirm, ends with session_reset
    started: bool = False  # Hermes began processing the opener (on_processing_start)
    ran: bool = False  # Hermes sent, edited or confirmed something for this query (not just queued it)
    failed: bool = False  # Hermes' processing of this query failed or was cancelled
    opener: str = ''  # the query that opened the chain; its completion ends the chain
    text: str = ''  # the reply so far: the latest stream segment or final send (replies are never chunked)
    stream: str | None = None  # message id of the live stream segment
    commentary: str = ''  # the latest interim commentary, the reply when Hermes delivered the answer that way
    notes: list = field(default_factory=list)  # media Hermes sent with the reply (text fallbacks), shown after it
    arrivals: list = field(default_factory=list)  # queries sent mid-chain that have not started their own turn
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
        # A v0 device cannot tell a push from the answer it waits for: pushes wait for that answer, this long at most.
        self._hold_seconds = float(setting('push_hold_seconds', 120))
        self._max_frame_mb = float(setting('max_frame_mb', 8))
        self.registry = Registry()
        keep = str(setting('outbox_persist', 'true')).strip().lower() not in ('0', 'false', 'no', 'off')
        self.outbox = Outbox(int(setting('outbox_max', 50)), float(setting('outbox_ttl_hours', 24)),
                             path=_outbox_path() if keep else None)
        self.broker = PhotoBroker(self._max_frame_mb)
        self._tokens, self._active_turn, self._user_locks = {}, {}, {}
        self._queries, self._delivered = {}, {}  # recent query id -> device; user -> recently delivered push ids
        self._resets = {}  # new_session query id -> (device, time), until its session_reset is sent
        self._approvals = {}  # user -> approvals sent to the devices and not yet answered there, oldest first
        self._approval_sessions = {}  # user -> Hermes session keys that have asked this user's devices for approval
        self._silent, self._speech = {}, OrderedDict()
        self._speech_slots = asyncio.Semaphore(2)
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
        register_routes(self, self._app)
        register_omi_routes(self, self._app)
        register_xiaozhi_routes(self, self._app)
        # Public aiohttp calls, not Hermes' private shared_ingress.bind_listener: glasses is not one of Hermes'
        # port-binding platforms, so its shared-listener mode never applies and the bind is the same.
        # Query credentials must never reach aiohttp's access log.
        runner = web.AppRunner(self._app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self._host, self._port).start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner
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
                if kind == 'interrupt' and conn.protocol == 1:  # the wearer's stop button: Hermes' own /stop
                    frame, kind = {'type': 'query', 'id': frame.get('id'), 'text': '/stop'}, 'query'
                if kind in ('query', 'new_session'):
                    if not (isinstance(frame.get('id'), str) and frame['id']):
                        frame['id'] = uuid4().hex
                    # Before the flush below: queued pushes must not reach a v0 device as this question's answer.
                    conn.asking[frame['id']] = time.monotonic()
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
                elif kind == 'approval_response' and conn.protocol == 1:
                    await self._button_approval(conn, frame)
                elif kind == 'ack' and conn.protocol == 1 and conn.capabilities.get('ack') is True:
                    message_id = frame.get('id')
                    if isinstance(message_id, str) and message_id in conn.unacked:
                        self._mark_delivered(identity.user, message_id)
                elif kind in ('photo', 'photo_error'):
                    self.broker.resolve(conn, frame)
                elif kind == 'new_session':
                    self._spawn(self._new_session(conn, frame['id']))
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
        if conn.held_task:
            conn.held_task.cancel()
        # Never settled or still held: keep its pushes for the next connection, unless another device already
        # received them.
        delivered = self._delivered.get(conn.identity.user, ())
        left = [*conn.pending, *conn.held, *conn.unacked.values()]
        self.outbox.merge(conn.identity.user, [item for item in left if item[3] not in delivered])
        self.outbox.forget([item[3] for item in left if item[3] in delivered])
        conn.pending, conn.held = [], []
        conn.unacked.clear()

    async def _new_session(self, conn, query_id=None):
        conn.last_photo_at = 0
        query_id = query_id or uuid4().hex
        self._resets[query_id] = (conn, time.monotonic())
        await self._process_query(conn, {'type': 'query', 'text': '/new', 'id': query_id}, reset=True)

    async def _ack_reset(self, query_id):
        conn, _ = self._resets.pop(query_id, (None, 0))
        if conn is None:
            return
        turn = self._active_turn.get(conn.identity.user)
        if turn and query_id in turn.arrivals:
            turn.arrivals.remove(query_id)  # answered: it was not folded into the running turn
        asked, conn = conn, self._live_socket(conn)
        if conn:
            await conn.send_json(protocol.session_reset())
        await self._answered(asked, query_id)

    def _live_socket(self, conn):
        """The device's socket: the one it asked on, or the one it reconnected with since."""
        live = self.registry.live(conn.identity.user)
        if conn in live:
            return conn
        return next((other for other in reversed(live) if other.identity == conn.identity and not other.slot), None)

    async def _process_query(self, conn, frame, reset=False):
        self._silent[(conn.identity.user, frame.get('id'))] = frame.get('speak') is False
        while len(self._silent) > 200:
            self._silent.pop(next(iter(self._silent)))
        text = frame.get('text', '')
        text = text.strip() if isinstance(text, str) else ''
        if conn.protocol == 1 and isinstance(frame.get('audio'), dict):
            try:
                from gateway.platforms.base import cache_audio_from_bytes_async
                from tools.transcription_tools import transcribe_audio
                data, ext = audio.decode(frame['audio'], self.broker.max_bytes)
                path = await cache_audio_from_bytes_async(data, ext)
                result = await asyncio.wait_for(asyncio.to_thread(transcribe_audio, path), 40)  # inside the HTTP turn wait
                text = result.get('transcript', '').strip() if result.get('success') else ''
                text = re.sub(r'^[\s/]+', '', text)  # speech never issues slash commands (the stop button is `interrupt`)
                if not text:
                    raise ValueError('Empty transcript')
                await conn.send_json(protocol.transcript(frame.get('id'), text))
            except Exception:
                await conn.send_json(protocol.error("I couldn't hear that."))
                await self._answered(conn, frame.get('id'))
                return
        if not text:
            await conn.send_json(protocol.error('Empty query.'))
            await self._answered(conn, frame.get('id'))
            return
        user = conn.identity.user
        # A yes or no while an approval waits is handled here (a typed /approve or /deny goes to Hermes as is).
        if self._approval_sessions.get(user) and not text.startswith('/'):
            hermes_words = hermes_approval_words()
            raw = text.lower()
            choice = hermes_words[raw] if raw in hermes_words else spoken_choice(text)
            if (raw in hermes_words or choice) and await self._answer_approval(conn, frame.get('id'), choice):
                return
        turn_id = frame.get('id')
        turn_id = turn_id if isinstance(turn_id, str) and turn_id else uuid4().hex
        self._queries[turn_id] = conn
        while len(self._queries) > 200:
            self._queries.pop(next(iter(self._queries)))
        # Hermes owns queued turns; on_processing_start selects their exact reply socket.
        turn = self._active_turn.setdefault(user, Turn(conn, turn_id, reset=reset))
        if turn.started and turn_id not in turn.ids:
            turn.arrivals.append(turn_id)  # Hermes may queue it, or fold it into the running turn (interrupt)
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
        raw = {k: v for k, v in frame.items() if k not in ('image', 'audio', 'data')}
        event = MessageEvent(
            text=text, message_type=MessageType.PHOTO if path else MessageType.TEXT,
            source=self.build_source(chat_id=user, chat_name=user, chat_type='dm', user_id=user, user_name=user),
            user_id=user, user_name=user, message_id=turn_id,
            media_urls=[path] if path else [], media_types=['image/jpeg'] if path else [], raw_message=raw)
        event._glasses_connection = conn  # retained through Hermes startup replay
        event._glasses_reset = reset
        await self.handle_message(event)
        if text.startswith(('/approve', '/deny')):
            self._live_approvals(user)

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
                for item in items:
                    _, text, origin, message_id = item
                    if self._holds(conn, origin):
                        self._hold(conn, item)
                    elif not await self._write_push(conn, item):
                        break
                    sent += 1
            finally:
                if sent < len(items):  # closed or cancelled: keep the rest queued, retry on the next frame
                    conn.flushed = False
                    delivered = self._delivered.get(user, ())  # another device already got these
                    self.outbox.merge(user, [item for item in items[sent:] if item[3] not in delivered])
                    self.outbox.forget([item[3] for item in items[sent:] if item[3] in delivered])

    def _mark_delivered(self, user, message_id):
        self._delivered.setdefault(user, deque(maxlen=500)).append(message_id)
        for conn in self.registry.live(user):
            conn.unacked.pop(message_id, None)
        self.outbox.sent(message_id)

    async def _write_push(self, conn, item, frame=None):
        _, text, origin, message_id = item
        frame = frame if frame is not None else protocol.push(conn.protocol, text, origin, message_id)
        if not await conn.send_json(frame):
            return False
        user = conn.identity.user
        if conn.slot:  # an HTTP answer: delivered once the answer is written (http_api), queued again if not
            conn.sent_items.append((item, frame))
            self.outbox.track(user, item)  # stays in the file until then, so a restart cannot lose it
        elif frame['type'] == 'push' and conn.capabilities.get('ack') is True:
            if message_id not in self._delivered.get(user, ()):
                conn.unacked[message_id] = item
                self.outbox.track(user, item)
        else:
            self._mark_delivered(user, message_id)
        if not conn.slot:  # an HTTP answer carries only the turn's own audio: no speech for its pushes
            self._speak(conn, text, frame['id'] if frame['type'] == 'approval_request' else message_id)
        return True

    async def _flush_later(self, conn, delay):
        await asyncio.sleep(delay)
        await self._flush_outbox(conn)

    def _waiting(self, conn):
        """The device has a question in flight. One unanswered past the hold limit (an interrupted query) stops counting."""
        now = time.monotonic()
        for query_id, asked in list(conn.asking.items()):
            if now - asked >= self._hold_seconds:
                del conn.asking[query_id]
        return bool(conn.asking)

    def _holds(self, conn, origin):
        # v0 has no push frame: a push sent while the device waits would be taken as its answer. Once pushes are
        # held, later ones queue behind them so the device still hears them in order.
        return conn.protocol == 0 and origin not in PROMPT_ORIGINS and (bool(conn.held) or self._waiting(conn))

    def _hold(self, conn, item):
        conn.held.append(item)
        self.outbox.track(conn.identity.user, item)
        if conn.held_task is None:
            conn.held_task = self._spawn(self._release_held_later(conn))

    async def _release_held_later(self, conn):
        await asyncio.sleep(self._hold_seconds)
        await self._release_held(conn)

    async def _release_held(self, conn):
        async with self._lock(conn.identity.user):
            await self._send_held(conn)

    async def _send_held(self, conn):
        """Send the held pushes in order. The caller holds the user's lock."""
        user = conn.identity.user
        task, conn.held_task = conn.held_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        items, conn.held, sent = conn.held, [], 0
        try:
            for item in items:
                if not await self._write_push(conn, item):
                    break
                sent += 1
        finally:
            if sent < len(items):  # closed or cancelled: the rest waits for the next connection
                delivered = self._delivered.get(user, ())
                self.outbox.merge(user, [item for item in items[sent:] if item[3] not in delivered])
                self.outbox.forget([item[3] for item in items[sent:] if item[3] in delivered])

    async def _answered(self, conn, query_id):
        """The device got its answer (or its error): pushes held behind it follow now."""
        if conn is None:
            return
        conn.asking.pop(query_id, None)
        if conn.held and not self._waiting(conn):
            await self._release_held(conn)

    async def _settle(self, user, query_ids):
        """A processing chain ended: its queries are answered or superseded (queued resets still wait)."""
        for conn in self.registry.live(user):
            for query_id in query_ids:
                if query_id not in self._resets:
                    await self._answered(conn, query_id)

    async def _push(self, user, content, origin, message_id, frame_builder=None):
        # The per-user lock keeps queued and live pushes in order. Devices whose protocol is not settled
        # yet hold the push until their flush; with no device reachable it waits in the outbox.
        async with self._lock(user):
            reached = False
            for conn in self.registry.live(user):
                item = (time.time(), content, origin, message_id)
                if not conn.flushed:
                    conn.pending.append(item)
                    self.outbox.track(user, item)
                    reached = True
                elif self._holds(conn, origin):
                    self._hold(conn, item)
                    if not self._waiting(conn):  # its question went stale: the older held pushes go first, now
                        await self._send_held(conn)
                    reached = True
                else:
                    frame = frame_builder(conn) if frame_builder else None
                    if await self._write_push(conn, item, frame):
                        if frame and frame['type'] == 'approval_request':
                            for approval in self._approvals.get(user, []):
                                if approval.request_id == frame['id']:
                                    approval.connections.add(conn)
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
        if origin == 'cron':
            content = _unwrap_cron(content)
        turn = self._active_turn.get(user)
        final = metadata.get('notify') and not metadata.get('_interim_send')
        owner = metadata.get('reply_to_message_id')  # stream-consumer writes name the query they belong to
        asker = self._queries.get(reply_to) if reply_to is not None else None
        if final and asker is not None and asker.identity.user == user and not (turn and reply_to in turn.ids):
            # A command answered inline while a turn runs (/stop, /status): the reply to that query alone.
            if turn and reply_to in turn.arrivals:
                turn.arrivals.remove(reply_to)
            if reply_to in self._resets:  # a busy new_session: v0 contract is session_reset alone
                await self._ack_reset(reply_to)
            else:
                await self._deliver_reply(user, self._queries[reply_to], reply_to, content)
        elif turn is None or origin is not None:
            return await self._push(user, content, origin or 'message', message_id)
        elif owner and owner in turn.ids and owner != turn.id:
            await self._status(user, 'working', content)  # a superseded query's late stream write
        else:
            if reply_to in (None, turn.id) and owner in (None, turn.id):
                turn.ran = True  # not a busy notice or late write for another query
            if metadata.get('glasses_media'):
                turn.notes.append(content)  # a media fallback follows the reply; it never replaces it
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
            turn.ran = True
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
            message_id = uuid4().hex
            self._silent[(user, message_id)] = self._silent.get((user, query_id), False)
            await self._push(user, text, 'reply', message_id)  # the asker went offline: deliver out of band
        elif not self._silent.get((user, query_id)):
            self._speak(conn, text, query_id)
        await self._answered(conn, query_id)

    def _speak(self, conn, text, ref):
        mimes = audio.output_mimes(conn.capabilities) if conn.protocol == 1 else []
        if mimes and not self._silent.get((conn.identity.user, ref)):
            self._spawn(self._send_speech(conn, text, ref, mimes))

    async def _send_speech(self, conn, text, ref, mimes):
        # The text sender may hold this lock; synthesis must start after it has released it.
        async with self._lock(conn.identity.user):
            pass
        for mime in mimes:  # the device's formats in its order: the next one if a conversion fails
            key = (text, mime)
            task = self._speech.get(key)
            if task is None:
                task = self._speech[key] = self._spawn(self._synthesise(text, mime))
                while len(self._speech) > 16:
                    self._speech.popitem(last=False)
            data = await asyncio.shield(task)
            if data:
                await conn.send_json(protocol.audio(ref, base64.b64encode(data).decode(), mime))
                return
            if self._speech.get(key) is task:
                self._speech.pop(key, None)  # a failure is not remembered: the next reply tries again

    async def _synthesise(self, text, mime):
        try:
            from tools.tts_tool import text_to_speech_tool
            async with self._speech_slots:  # at most two syntheses at once, e.g. when a full queue is flushed
                result = json.loads(await asyncio.wait_for(asyncio.to_thread(text_to_speech_tool, text), 60))
            paths = result.get('file_paths') or ([result['file_path']] if result.get('file_path') else [])
            if result.get('success') is False or not paths:
                raise ValueError('No TTS files')
            return await audio.encode_files(paths, mime)
        except Exception:
            logger.debug('glasses: speech unavailable', exc_info=False)
            return None

    async def _finish_query(self, user, turn):
        """Answer the query the chain is on, exactly once."""
        if turn.reset:  # v0 contract: new_session is answered by session_reset alone
            # Only once Hermes ran the /new: during gateway startup it queues the event (processing hooks still
            # fire, with nothing run) and replays it later, so an early session_reset would let the device's
            # next query race the real reset.
            conn = self._live_socket(turn.conn)
            if turn.ran:
                self._resets.pop(turn.id, None)
                if conn:
                    await conn.send_json(protocol.session_reset())
                await self._answered(turn.conn, turn.id)
            elif turn.failed:
                self._resets.pop(turn.id, None)
                if conn:
                    await conn.send_json(protocol.error('New session failed.'))
                await self._answered(turn.conn, turn.id)
            elif turn.id in self._resets:  # only queued: keep it fresh for the inline confirm of its replay
                self._resets[turn.id] = (self._resets[turn.id][0], time.monotonic())
        elif turn.text or turn.commentary or turn.notes:
            text = '\n'.join([turn.text or turn.commentary, *turn.notes]).strip()
            await self._deliver_reply(user, turn.conn, turn.id, text)
        await self._status(user, 'idle')

    async def _send_exec_approval_prompt(self, prompt):
        """A dangerous-command approval, said the way a voice user can answer it: yes or no. Each one is tied to its
        Hermes request id, so a spoken answer can only ever resolve that request."""
        from tools.approval import list_gateway_approvals
        user = str(prompt.chat_id)
        if user not in {identity.user for identity in self._tokens.values()}:
            return SendResult(success=False, error='Unknown glasses user')
        self._live_approvals(user)  # forget requests that ended, so they can never be confused with this one
        self._approval_sessions.setdefault(user, set()).add(prompt.session_key)
        request_id = getattr(prompt, 'request_id', None)
        if not request_id:  # Hermes before 0.23 does not pass it: take the one new request in the session's queue
            known = {a.request_id for a in self._approvals.get(user, [])}
            new = [d.get('request_id') for d in list_gateway_approvals(prompt.session_key)
                   if d.get('request_id') and d.get('request_id') not in known]
            if len(new) != 1:
                return SendResult(success=False, error='Approval request not identifiable')  # Hermes' text prompt
            request_id = new[0]
        approval = Approval(prompt.session_key, request_id, list(prompt.choices), time.monotonic())
        self._approvals.setdefault(user, []).append(approval)
        command = ' '.join(str(prompt.command).split())
        command = command if len(command) <= 90 else command[:87] + '...'
        reason = ' '.join(str(prompt.description or '').split()).rstrip('.')
        reason = reason[:1].upper() + reason[1:]
        allow = 'Say yes to allow it once, or no to block it.' if 'once' in prompt.choices else 'Say no to block it.'
        text = f'Hermes wants to run: {command}.' + (f' {reason}.' if reason else '') + f' {allow} (Typed: /approve or /deny.)'
        def build(conn):
            if conn.protocol == 1 and conn.capabilities.get('buttons') is True:
                return protocol.approval_request(request_id, text, prompt.command, prompt.description,
                                                 list(prompt.choices))
            return protocol.push(conn.protocol, text, 'approval', message_id)
        message_id = uuid4().hex
        result = await self._push(user, text, 'approval', message_id, build)
        approval.shown = True
        return result

    async def _close_approval(self, user, approval, choice=None, except_conn=None):
        for other in self.registry.live(user):
            if other is not except_conn and other in approval.connections:
                await other.send_json(protocol.approval_closed(approval.request_id, choice))

    async def _button_approval(self, conn, frame):
        from tools.approval import resolve_gateway_approval
        user, request_id, choice = conn.identity.user, frame.get('id'), frame.get('choice')
        approval = next((a for a in self._approvals.get(user, [])
                         if isinstance(request_id, str) and a.request_id == request_id), None)
        message = 'Unknown approval.'
        ok = False
        if approval:
            if approval not in self._live_approvals(user):
                message = 'Approval expired.'
            elif not isinstance(choice, str) or choice not in approval.choices:
                message = 'Invalid choice.'
            elif resolve_gateway_approval(approval.session_key, choice, request_id=approval.request_id):
                self._approvals[user].remove(approval)
                if not self._approvals[user]:
                    self._approvals.pop(user)
                ok = True
                message = "Okay, I won't run it." if choice == 'deny' else 'Okay, going ahead.'
            else:
                message = 'Approval expired.'
        await conn.send_json(protocol.approval_result(request_id, ok, message, choice if ok else None))
        if ok:
            await self._close_approval(user, approval, choice, conn)

    def _live_approvals(self, user):
        """This user's approvals that Hermes still waits for (not timed out, not answered by a typed /approve)."""
        from tools.approval import list_gateway_approvals
        previous = self._approvals.get(user, [])
        live = [a for a in previous
                if any(d.get('request_id') == a.request_id for d in list_gateway_approvals(a.session_key))]
        for approval in previous:
            if approval not in live and approval.connections:
                self._spawn(self._close_approval(user, approval))
        if live:
            self._approvals[user] = live
        else:
            self._approvals.pop(user, None)
        return live

    async def _answer_approval(self, conn, query_id, choice):
        """Answer the one approval this user's devices are waiting on with a spoken yes or no (`choice` None: an
        approval word that asks for more than once, like "always").

        False (the words go to Hermes) when no approval is waiting or a clarify question is open on the session
        the words go to (Hermes answers that first). Hermes itself takes a bare "yes" as /approve for its OLDEST
        waiting request, so whenever a request waits and it is not certain which one the words answer (two
        waiting, one this adapter could not identify, one that ended mid-answer, one not yet shown), the words
        are kept here, nothing is resolved, and the device is told how to answer."""
        from tools.approval import list_gateway_approvals, resolve_gateway_approval
        from tools import clarify_gateway
        user = conn.identity.user
        source = self.build_source(chat_id=user, chat_name=user, chat_type='dm', user_id=user, user_name=user)
        own_session = self._source_session_key(source)
        sessions = self._approval_sessions.get(user, set()) | {own_session}
        queued = {d.get('request_id') for key in sessions for d in list_gateway_approvals(key)}
        if not queued:
            self._live_approvals(user)
            return False
        if clarify_gateway.has_pending(own_session):
            return False  # the agent asked a question: Hermes routes a yes or no to its clarify handler first
        known = len(self._approvals.get(user, []))
        live = self._live_approvals(user)
        approval = live[0] if len(live) == 1 else None
        certain = bool(approval and approval.shown and known == 1 and queued == {approval.request_id})
        if certain and choice is None:
            said = 'By voice I can only allow a command once. Say yes or no.'
        elif certain and choice not in approval.choices:
            said = 'This command can only be blocked. Say no to block it.'
        elif certain and resolve_gateway_approval(approval.session_key, choice, request_id=approval.request_id):
            self._approvals.pop(user, None)
            await self._close_approval(user, approval, choice)  # the speaking device's card closes too
            said = 'Okay, going ahead.' if choice == 'once' else "Okay, I won't run it."
        else:
            self._live_approvals(user)
            said = 'I was not sure which command that answers, so I did not act on it. Use /approve or /deny.'
        await self._deliver_reply(user, conn, query_id, said)
        return True

    async def send_slash_confirm(self, chat_id, title, message, session_key, confirm_id, metadata=None):
        # The device's new_session button is the user's explicit confirmation of the /new it sends; anything
        # else (a spoken "/new", /reload-mcp) falls back to Hermes' text prompt.
        user, now = str(chat_id), time.monotonic()
        turn = self._active_turn.get(user)
        # While another turn runs, Hermes handles /new inline (no turn of its own): find that fresh request.
        inline = next((query_id for query_id, (conn, at) in self._resets.items()
                       if conn.identity.user == user and now - at < 30 and not (turn and query_id in turn.ids)), None)
        from tools import slash_confirm
        pending = slash_confirm.get_pending(session_key) or {}
        if pending.get('command') not in ('new', 'reset') or not ((turn and turn.reset) or inline):
            return SendResult(success=False, error='Not supported')
        await slash_confirm.resolve(session_key, confirm_id, 'once')
        if turn and turn.reset:
            turn.ran = True
        else:
            await self._ack_reset(inline)  # no completion hook follows an inline command
        return SendResult(success=True)

    # Glasses cannot show media yet (M0): Hermes' text fallbacks for it become notes after the reply.
    async def send_image(self, *args, metadata=None, **kwargs):
        return await super().send_image(*args, metadata=_as_media(metadata), **kwargs)

    async def send_multiple_images(self, *args, metadata=None, **kwargs):
        return await super().send_multiple_images(*args, metadata=_as_media(metadata), **kwargs)

    async def send_image_file(self, *args, metadata=None, **kwargs):
        return await super().send_image_file(*args, metadata=_as_media(metadata), **kwargs)

    async def send_voice(self, *args, metadata=None, **kwargs):
        return await super().send_voice(*args, metadata=_as_media(metadata), **kwargs)

    async def send_video(self, *args, metadata=None, **kwargs):
        return await super().send_video(*args, metadata=_as_media(metadata), **kwargs)

    async def send_document(self, *args, metadata=None, **kwargs):
        return await super().send_document(*args, metadata=_as_media(metadata), **kwargs)

    async def send_clarify(self, *args, metadata=None, **kwargs):
        return await super().send_clarify(*args, metadata={**(metadata or {}), 'glasses_prompt': 'clarify'}, **kwargs)

    async def _status(self, user, state, detail=None):
        for conn in self._targets(str(user)):
            await self._status_to(conn, state, detail)

    async def _status_to(self, conn, state, detail=None):
        frame = protocol.status(state, detail)
        if conn.protocol != 1 or conn.last_status == frame or conn.ws.closed:
            return
        if state == 'thinking' and conn.last_status.get('state') == 'working':
            return  # the typing refresh must not hide the latest activity detail
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
            turn.conn, turn.id, turn.reset, turn.ran, turn.text, turn.stream, turn.commentary, turn.notes = (
                conn, event.message_id, reset, False, '', None, '', [])
            turn.failed = False
            turn.ids.add(event.message_id)
            if event.message_id in turn.arrivals:
                turn.arrivals.remove(event.message_id)
        await self._status(user, 'thinking')

    async def on_processing_complete(self, event, outcome):
        user = event.source.chat_id
        turn = self._active_turn.get(user)
        # Follow-ups complete inside the opener's processing, before their reply is sent; only the opener's
        # completion (after Hermes' final send) ends the chain.
        if turn and turn.started and event.message_id == turn.opener:
            turn.failed = outcome is not ProcessingOutcome.SUCCESS
            # A mid-chain query that never started and is not queued was folded into this run by Hermes'
            # interrupt: the reply answers the latest such query, on its own device.
            folded = [q for q in turn.arrivals if q in self._queries and not self._queued(q)]
            if folded and not turn.reset:
                asked, turn.id, turn.conn = turn.conn, folded[-1], self._queries[folded[-1]]
                if asked is not turn.conn:
                    await self._status_to(asked, 'idle')
            await self._finish_query(user, turn)
            self._active_turn.pop(user, None)
            # Not for a pass that only queued the query (Hermes' startup-restore gate): it replays and answers later.
            if turn.ran or turn.failed:
                await self._settle(user, {*turn.ids, turn.id, *folded})

    def _queued(self, query_id):
        return any(getattr(event, 'message_id', None) == query_id for event in self._pending_messages.values())

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

    async def device_action(self, user, kind, payload):
        if kind not in ('display', 'haptic'):
            raise ValueError('Unknown device action')
        async def deliver():
            capability = 'display' if kind == 'display' else 'haptics'
            targets = [c for c in self.registry.live(user)
                       if c.protocol == 1 and c.capabilities.get(capability) is True]
            turn = self._active_turn.get(user)
            if turn and turn.conn in targets:
                targets = [turn.conn]
            frame = protocol.display(**payload) if kind == 'display' else protocol.haptic(**payload)
            count = 0
            for conn in targets:
                count += bool(await conn.send_json(frame))
            return count
        if asyncio.get_running_loop() is self._loop:
            return await deliver()
        future = asyncio.run_coroutine_threadsafe(deliver(), self._loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), 5)
        except TimeoutError:
            future.cancel()
            return 0
