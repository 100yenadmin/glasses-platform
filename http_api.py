"""Short-lived v1 connections for devices that cannot keep a WebSocket open."""
import asyncio
import time
from uuid import uuid4

from aiohttp import web
from gateway.platforms._shared import extra_or_secret

from . import audio
from .devices import Connection, authenticate

AUDIO_TIMEOUT = 15
PROMPT_ORIGINS = ('approval', 'clarify')


class HttpSocket:
    """The Connection socket interface, with frames retained until the HTTP answer."""

    def __init__(self):
        self.closed = False
        self.frames = []
        self.changed = asyncio.Event()

    async def send_json(self, frame):
        if self.closed:
            raise ConnectionError('HTTP request finished')
        self.frames.append(dict(frame))
        self.changed.set()

    async def close(self, code=1000, message=b''):
        self.closed = True
        self.changed.set()


def _capabilities(body):
    capabilities = (body or {}).get('capabilities')
    capabilities = dict(capabilities) if isinstance(capabilities, dict) else {}
    capabilities.pop('camera', None)  # a request cannot answer capture_photo
    capabilities.pop('ack', None)  # the HTTP answer itself is the delivery
    for action in ('display', 'haptics'):  # the answer carries no display or haptic frames
        capabilities.pop(action, None)
    return capabilities


def _is_prompt(frame):
    return frame.get('type') == 'approval_request' or (
        frame.get('type') == 'push' and frame.get('origin') in PROMPT_ORIGINS)


def _is_push(frame):
    return frame.get('type') in ('push', 'approval_request')


def _terminal(frames, turn_id, start=0):
    """What ends this turn's request: its own reply first, else a prompt or an error that arrived after the query
    was sent (prompts and errors queued before it belong to earlier turns and are returned as pushes)."""
    later = frames[start:]
    reply = next((frame for frame in later if frame.get('type') == 'response' and frame.get('turn') == turn_id), None)
    return reply or next((frame for frame in later if _is_prompt(frame) or frame.get('type') == 'error'), None)


async def _wait_for(socket, ready, seconds):
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        # Clear before checking: a frame arriving before wait() still wakes it.
        socket.changed.clear()
        if ready(socket.frames) or socket.closed:
            return
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(socket.changed.wait(), remaining)
        except asyncio.TimeoutError:
            return


async def _finish(adapter, conn):
    """Close the request's connection once. _push holds this same lock, so each push is either in the snapshot or
    queued for the next request."""
    if not conn.ws.closed or adapter.registry.by_user.get(conn.identity.user, {}).get(conn.slot) is conn:
        async with adapter._lock(conn.identity.user):
            await conn.ws.close()
            adapter.registry.remove(conn)
        adapter._release(conn)
    return list(conn.ws.frames)


def _reply(frames, turn_id):
    return next((frame.get('text') for frame in frames
                 if frame.get('type') == 'response' and frame.get('turn') == turn_id), None)


def _requeue(adapter, conn, reply=None, turn_id=None, shown=None):
    """Queue again every push written to this request that the device did not get (the original items, with their
    ids and times), and the turn's reply if the answer was lost. `shown` picks the frames the written answer carried:
    those are delivered. Repeating the same id is harmless: the queue keeps one copy."""
    user = conn.identity.user
    delivered = adapter._delivered.get(user, ())  # another device already got these
    items, done = [], []
    for item, frame in conn.sent_items:
        if shown is not None and shown(frame):
            adapter._mark_delivered(user, item[3])
        elif item[3] in delivered:
            done.append(item[3])
        else:
            items.append(item)
    adapter.outbox.forget(done)
    if reply:
        items.append((time.time(), reply, 'reply', f'reply-{turn_id}'))
    adapter.outbox.merge(user, items)


async def _send(request, adapter, conn, response, shown, reply=None, turn_id=None):
    """Write the answer now, so a client that went away is noticed and nothing it was sent is lost. Pushes count as
    delivered only once the answer is written, and only those it shows."""
    transport = request.transport
    try:
        if transport is None or transport.is_closing():
            raise ConnectionResetError('client went away')
        await response.prepare(request)
        await response.write_eof()
    except (ConnectionError, RuntimeError, asyncio.CancelledError) as exc:
        _requeue(adapter, conn, reply, turn_id)
        if isinstance(exc, asyncio.CancelledError):
            raise
    else:
        _requeue(adapter, conn, shown=shown)
    return response


async def handle_approval(adapter, conn, body):
    """`POST /approval`: the same checks as a WebSocket `approval_response` (this user's live request, Hermes's own
    choices); answers with the `approval_result`."""
    await adapter._button_approval(conn, {'id': body.get('id'), 'choice': body.get('choice')})
    result = next((frame for frame in conn.ws.frames if frame.get('type') == 'approval_result'), None)
    return web.json_response(result or {'type': 'approval_result', 'id': body.get('id'), 'ok': False,
                                        'message': 'Unknown approval.'})


class HttpApi:
    def __init__(self, adapter):
        self.adapter = adapter
        self.turn_timeout = float(extra_or_secret(
            adapter.config.extra, 'turn_timeout', 'GLASSES_TURN_TIMEOUT', 50))

    def _connection(self, request, body=None):
        auth = request.headers.get('Authorization', '')
        token = auth[7:] if auth.lower().startswith('bearer ') else request.query.get('token')
        identity = authenticate(token, self.adapter._tokens)
        if identity is None:
            raise web.HTTPUnauthorized(text='{"error":"unauthorized"}', content_type='application/json')
        # Its own registry slot: an HTTP request never replaces the device's WebSocket or another open request.
        return Connection(HttpSocket(), identity, protocol=1, slot=f'{identity.device}#http-{uuid4().hex}',
                          capabilities=_capabilities(body))

    async def _body(self, request):
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text='{"error":"malformed JSON"}', content_type='application/json')
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='{"error":"expected an object"}', content_type='application/json')
        return body

    async def turn(self, request):
        conn = self._connection(request)
        body = await self._body(request)
        text = body.get('text')
        if not (isinstance(text, str) and text.strip()) and not isinstance(body.get('audio'), dict):
            raise web.HTTPBadRequest(text='{"error":"text or audio required"}', content_type='application/json')
        conn.capabilities = _capabilities(body)
        turn_id = body.get('id')
        turn_id = turn_id if isinstance(turn_id, str) and turn_id else uuid4().hex
        frame = {key: body[key] for key in ('text', 'audio', 'image', 'speak') if key in body}
        frame.update(type='query', id=turn_id)
        conn.asking[turn_id] = time.monotonic()
        start = 0
        try:
            await self.adapter.registry.add(conn)
            await self.adapter._flush_outbox(conn)
            start = len(conn.ws.frames)  # queued pushes end here; this turn's frames follow
            self.adapter._spawn(self.adapter._process_query(conn, frame))
            await _wait_for(conn.ws, lambda frames: _terminal(frames, turn_id, start) is not None, self.turn_timeout)
            terminal = _terminal(conn.ws.frames, turn_id, start)
            speaks = body.get('speak') is not False and audio.output_mimes(conn.capabilities)
            if speaks and terminal is not None and terminal.get('type') == 'response':
                await _wait_for(conn.ws, lambda frames: any(
                    item.get('type') == 'audio' and item.get('ref') == turn_id for item in frames), AUDIO_TIMEOUT)
            frames = await _finish(self.adapter, conn)
            response, reply, shown = self._turn_answer(request, frames, turn_id, start)
        except BaseException:
            frames = await _finish(self.adapter, conn)
            _requeue(self.adapter, conn, _reply(frames, turn_id), turn_id)
            raise
        return await _send(request, self.adapter, conn, response, shown, reply, turn_id)

    def _turn_answer(self, request, frames, turn_id, start):
        terminal = _terminal(frames, turn_id, start)
        result = {'id': turn_id, 'status': 'pending', 'pushes': []}
        prompts = []
        if terminal is not None:
            if _is_prompt(terminal):
                prompts = [item for item in frames if _is_prompt(item)]
                result.update(status='needs_answer', prompts=prompts)
            else:
                result.update(status='done', text=terminal.get('text', terminal.get('message', '')))
                transcript = next((item for item in frames if item.get('type') == 'transcript'
                                   and item.get('turn') == turn_id), None)
                if transcript is not None:
                    result['transcript'] = transcript.get('text', '')
                audio = next((item for item in frames if item.get('type') == 'audio'
                              and item.get('ref') == turn_id), None)
                if audio is not None:
                    result['audio'] = {'data': audio.get('data'), 'mime': audio.get('mime')}
        result['pushes'] = [item for item in frames if _is_push(item) and item not in prompts]
        status = 202 if result['status'] == 'pending' else 200
        reply = result.get('text') if result['status'] == 'done' and terminal.get('type') == 'response' else None
        if 'text/plain' in request.headers.get('Accept', '').lower():
            # Only the prompts are in a plain-text body: every other push stays queued for /pending.
            text = '\n'.join(str(item.get('text', '')) for item in prompts) if prompts else result.get('text', '')
            return (web.Response(text=text, status=status, content_type='text/plain'), reply,
                    lambda frame: frame in prompts)
        return web.json_response(result, status=status), reply, lambda frame: True

    async def pending(self, request):
        conn = self._connection(request)
        try:
            wait = float(request.query.get('wait', 25))
            if wait != wait:  # NaN is not a deadline.
                raise ValueError
        except ValueError:
            raise web.HTTPBadRequest(text='{"error":"invalid wait"}', content_type='application/json')
        wait = max(0, min(55, wait))
        try:
            await self.adapter.registry.add(conn)
            await self.adapter._flush_outbox(conn)
            await _wait_for(conn.ws, lambda frames: any(_is_push(item) for item in frames), wait)
            frames = await _finish(self.adapter, conn)
            response = web.json_response({'items': [item for item in frames if _is_push(item)]})
        except BaseException:
            await _finish(self.adapter, conn)
            _requeue(self.adapter, conn)
            raise
        return await _send(request, self.adapter, conn, response, lambda frame: True)

    async def approval(self, request):
        # Not registered as a live device: it only carries this one answer, so no push can land in it and be lost.
        conn = self._connection(request)
        body = await self._body(request)
        conn.capabilities = _capabilities(body)
        return await handle_approval(self.adapter, conn, body)


def register_routes(adapter, app):
    api = HttpApi(adapter)
    app.router.add_post('/turn', api.turn)
    app.router.add_get('/pending', api.pending)
    app.router.add_post('/approval', api.approval)
