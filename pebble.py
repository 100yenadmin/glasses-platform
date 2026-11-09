"""Bobby's phone-side wire protocol over a short-lived Hermes v1 connection."""
import asyncio
import re
import time
from uuid import uuid4

from aiohttp import web
from gateway.platforms._shared import extra_or_secret

from .devices import Connection, authenticate
from .http_api import HttpSocket, _finish, _is_push, _reply, _requeue, _terminal, _wait_for

WIDGET = '<<!!WIDGET:'


def _text(text, final=False):
    text = re.sub(r'<<!!WIDGET:.*?!!>>', '', text, flags=re.DOTALL)
    if not final:
        # Hold incomplete widgets (including their opening prefix) across cumulative deltas.
        start = text.find(WIDGET)
        if start >= 0:
            text = text[:start]
        for size in range(min(len(text), len(WIDGET) - 1), 0, -1):
            if text.endswith(WIDGET[:size]):
                return text[:-size]
    return text


class BobbyApi:
    def __init__(self, adapter):
        self.adapter = adapter
        self.wait = float(extra_or_secret(adapter.config.extra, 'bobby_wait', 'GLASSES_BOBBY_WAIT', 45))

    def _identity(self, request):
        return authenticate(request.match_info['token'], self.adapter._tokens)

    async def quota(self, request):
        if self._identity(request) is None:
            raise web.HTTPUnauthorized()
        return web.json_response({'used': 0, 'remaining': 1000000, 'hasSubscription': True})

    async def query(self, request):
        identity = self._identity(request)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if identity is None:
            await ws.send_str('d')
            await ws.close(code=1008, message=b'Unauthorized')
            return ws
        prompt = request.query.get('prompt', '')
        if not prompt.strip():
            await ws.send_str('wNo question.')
            await ws.send_str('d')
            await ws.close()
            return ws
        conn = Connection(HttpSocket(), identity, protocol=1, capabilities={},
                          slot=f'{identity.device}#bobby-{uuid4().hex}')
        turn_id = str(uuid4())
        conn.asking[turn_id] = time.monotonic()
        shown, done, frames = [], False, []

        async def receive():
            # Bobby only answers action requests, which this endpoint never emits.
            try:
                async for _ in ws:
                    pass
            finally:
                await conn.ws.close()

        reader = asyncio.create_task(receive())
        try:
            await self.adapter.registry.add(conn)
            await self.adapter._flush_outbox(conn)
            start = len(conn.ws.frames)
            await ws.send_str('fThinking…')
            self.adapter._spawn(self.adapter._process_query(
                conn, {'type': 'query', 'id': turn_id, 'text': prompt}))
            previous, cursor = '', start
            deadline = asyncio.get_running_loop().time() + self.wait

            async def chunk(text, final=False):
                nonlocal previous
                visible = _text(text, final)
                # Hermes can begin a new cumulative segment after a tool call.
                extra = visible[len(previous):] if visible.startswith(previous) else visible
                if extra:
                    await ws.send_str('c' + extra)
                previous = visible

            while not conn.ws.closed:
                await _wait_for(conn.ws, lambda items: len(items) > cursor,
                                max(0, deadline - asyncio.get_running_loop().time()))
                for frame in conn.ws.frames[cursor:]:
                    if frame.get('type') == 'response_delta' and frame.get('turn') == turn_id:
                        await chunk(frame.get('text', ''))
                cursor = len(conn.ws.frames)
                if _terminal(conn.ws.frames, turn_id, start) is not None:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    break
            frames = await _finish(self.adapter, conn)
            if conn.ws.closed and (ws.closed or reader.done()):
                raise ConnectionResetError('Bobby disconnected')
            terminal = _terminal(frames, turn_id, start)
            if terminal is None:
                await ws.send_str('cStill working; ask again in a moment for the answer.')
            elif terminal.get('type') == 'response':
                await chunk(terminal.get('text', ''), final=True)
            else:
                await ws.send_str(('w' if terminal.get('type') == 'error' else 'c') +
                                  _text(terminal.get('text', terminal.get('message', '')), final=True))
                shown.append(terminal)
            for frame in frames:
                if _is_push(frame) and frame not in shown:
                    origin = frame.get('origin', 'approval')
                    tag = 'Reminder' if origin == 'cron' else origin.capitalize()
                    await ws.send_str('c\n\n' + _text(tag + ': ' + frame.get('text', ''), final=True))
                    shown.append(frame)
            await ws.send_str('d')
            done = True
            await ws.send_str('t' + turn_id)
            await ws.close(code=1000)
        except (ConnectionError, RuntimeError, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            frames = await _finish(self.adapter, conn)
            _requeue(self.adapter, conn, None if done else _reply(frames, turn_id), turn_id,
                     shown=(lambda frame: frame in shown) if done else None)
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        return ws


def register_routes(adapter, app):
    api = BobbyApi(adapter)
    app.router.add_get('/bobby/{token}/query', api.query)
    app.router.add_get('/bobby/{token}/quota', api.quota)
