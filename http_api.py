"""Short-lived v1 connections for devices that cannot keep a WebSocket open."""
import asyncio
import time
from uuid import uuid4

from aiohttp import web
from gateway.platforms._shared import extra_or_secret

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


def _is_prompt(frame):
    return frame.get('type') == 'approval_request' or (
        frame.get('type') == 'push' and frame.get('origin') in PROMPT_ORIGINS)


def _is_push(frame):
    return frame.get('type') in ('push', 'approval_request')


def _terminal(frames, turn_id):
    return next((frame for frame in frames if _is_prompt(frame) or
                 frame.get('type') in ('error', 'session_reset') or
                 (frame.get('type') == 'response' and frame.get('turn') == turn_id)), None)


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
    # _push holds this same lock: each push lands in this snapshot or the outbox.
    async with adapter._lock(conn.identity.user):
        await conn.ws.close()
        adapter.registry.remove(conn)
        frames = list(conn.ws.frames)
    adapter._release(conn)
    return frames


async def handle_approval(adapter, conn, body):
    # TODO(approval lane): validate and dispatch the shared approval_response handler.
    return web.json_response({'error': 'not yet'}, status=501)


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
        capabilities = (body or {}).get('capabilities')
        return Connection(HttpSocket(), identity, protocol=1,
                          capabilities=capabilities if isinstance(capabilities, dict) else {})

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
        capabilities = body.get('capabilities')
        conn.capabilities = capabilities if isinstance(capabilities, dict) else {}
        turn_id = body.get('id')
        turn_id = turn_id if isinstance(turn_id, str) and turn_id else uuid4().hex
        frame = {key: body[key] for key in ('text', 'audio', 'image', 'speak') if key in body}
        frame.update(type='query', id=turn_id)
        conn.asking[turn_id] = time.monotonic()
        try:
            await self.adapter.registry.add(conn)
            await self.adapter._flush_outbox(conn)
            self.adapter._spawn(self.adapter._process_query(conn, frame))
            await _wait_for(conn.ws, lambda frames: _terminal(frames, turn_id) is not None, self.turn_timeout)
            terminal = _terminal(conn.ws.frames, turn_id)
            if terminal is not None and not _is_prompt(terminal) and conn.capabilities.get('audio_out'):
                await _wait_for(conn.ws, lambda frames: any(
                    item.get('type') == 'audio' and item.get('ref') == turn_id for item in frames), AUDIO_TIMEOUT)
        finally:
            frames = await _finish(self.adapter, conn)
        terminal = _terminal(frames, turn_id)
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
                    result['audio'] = {key: audio[key] for key in ('data', 'mime')}
        result['pushes'] = [item for item in frames if item.get('type') == 'push' and item not in prompts]
        status = 202 if result['status'] == 'pending' else 200
        if 'text/plain' in request.headers.get('Accept', '').lower():
            text = '\n'.join(str(item.get('text', '')) for item in prompts) if prompts else result.get('text', '')
            return web.Response(text=text, status=status, content_type='text/plain')
        return web.json_response(result, status=status)

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
        finally:
            frames = await _finish(self.adapter, conn)
        return web.json_response({'items': [item for item in frames if _is_push(item)]})

    async def approval(self, request):
        conn = self._connection(request)
        body = await self._body(request)
        try:
            await self.adapter.registry.add(conn)
            return await handle_approval(self.adapter, conn, body)
        finally:
            await _finish(self.adapter, conn)


def register_routes(adapter, app):
    api = HttpApi(adapter)
    app.router.add_post('/turn', api.turn)
    app.router.add_get('/pending', api.pending)
    app.router.add_post('/approval', api.approval)
