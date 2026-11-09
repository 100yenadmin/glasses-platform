"""Asyncio protocol-v1 device clients. Requires Python 3.11+ and aiohttp."""
import asyncio
import inspect
import json
import random
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp


class HermesGlassesError(Exception):
    def __init__(self, message, code='server'):
        super().__init__(message)
        self.code = code


class Interrupted(HermesGlassesError):
    def __init__(self):
        super().__init__('Query interrupted.', 'interrupted')


@dataclass
class TurnResult:
    id: str
    text: str
    transcript: str | None = None
    audio: dict | None = None


class HermesGlassesClient:
    def __init__(self, url, token, device, app=None, reconnect=True):
        self.url, self.token, self.device = url, token, device
        self.app, self.reconnect = app, reconnect
        self.on_capture_photo = None
        self.ready = False
        self.handshake = None
        self._listeners, self._queries, self._approvals = {}, {}, {}
        self._interrupts = set()
        self._prefix, self._sequence = uuid4().hex[:12], 0
        self._closed, self._backoff = False, 1
        self._runner = self._retry = self._connection = self._reset = None
        self._socket = self._outbox = None
        self._callbacks = set()

    def on(self, event, fn):
        listeners = self._listeners.setdefault(event, [])
        if fn not in listeners:
            listeners.append(fn)
        return lambda: self.off(event, fn)

    def off(self, event, fn):
        listeners = self._listeners.get(event, [])
        if fn in listeners:
            listeners.remove(fn)

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._callbacks.add(task)
        task.add_done_callback(self._callbacks.discard)
        return task

    async def _call_listener(self, awaitable):
        try:
            await awaitable
        except Exception:
            pass  # Application listeners must not stop the transport.

    def _emit(self, event, frame):
        for fn in tuple(self._listeners.get(event, [])):
            try:
                result = fn(frame)
                if inspect.isawaitable(result):
                    self._spawn(self._call_listener(result))
            except Exception:
                pass

    async def connect(self):
        if self.ready:
            return self.handshake
        if self._connection is None:
            self._closed = False
            if self._retry:
                self._retry.cancel()
                self._retry = None
            self._connection = asyncio.get_running_loop().create_future()
            self._runner = asyncio.create_task(self._run())
        return await asyncio.shield(self._connection)

    async def _write(self, ws, outbox):
        try:
            while True:
                await ws.send_str(await outbox.get())
        except (aiohttp.ClientError, ConnectionError, RuntimeError):
            await ws.close()

    async def _run(self):
        writer = timer = None
        code, invalid = 1006, False
        try:
            try:
                parts = urlsplit(self.url)
                if parts.scheme not in ('ws', 'wss') or not parts.hostname:
                    raise ValueError()
                parts.port  # Validate malformed ports before attempting transport/retries.
            except (ValueError, TypeError):
                invalid = True
                raise
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
                async with asyncio.timeout(10):
                    ws = await session.ws_connect(self.url, headers={'Authorization': 'Bearer ' + self.token})
                async with ws:
                    self._socket, self._outbox = ws, asyncio.Queue()
                    writer = asyncio.create_task(self._write(ws, self._outbox))
                    timer = asyncio.get_running_loop().call_later(10, lambda: self._spawn(ws.close()))
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            frame = json.loads(message.data)
                        except ValueError:
                            continue
                        if isinstance(frame, dict):
                            self._receive(frame)
                            if self.ready and timer:
                                timer.cancel()
                    code = ws.close_code or 1006
        except asyncio.CancelledError:
            raise
        except Exception:
            self._emit('error', {'type': 'error', 'message': 'Invalid WebSocket URL.' if invalid
                                else 'WebSocket transport error.'})
        finally:
            if timer:
                timer.cancel()
            if writer:
                writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)
            if invalid:
                self._closed = True
            self._lost(1000 if self._closed else code)
            self._runner = None
            if code != 4401:
                self._schedule_retry()

    def _lost(self, code):
        self.ready = False
        self._socket = self._outbox = None
        error = HermesGlassesError('Disconnected.', 'disconnected')
        self._settle(self._connection, error=error)
        self._connection = None
        for id in list(self._queries):
            self._finish(id, error)
        self._interrupts.clear()
        for future in self._approvals.values():
            self._settle(future, error=error)
        self._approvals.clear()
        self._settle(self._reset, error=error)
        self._reset = None
        self._emit('disconnected', {'code': code, 'reason': 'Disconnected.'})

    def _schedule_retry(self):
        if self.reconnect and not self._closed:
            delay = self._backoff * (0.75 + random.random() * 0.25)
            self._backoff = min(30, self._backoff * 2)
            self._retry = asyncio.get_running_loop().call_later(delay, lambda: self._spawn(self._reconnect()))

    async def _reconnect(self):
        try:
            await self.connect()
        except HermesGlassesError:
            pass

    async def disconnect(self):
        self._closed = True
        if self._retry:
            self._retry.cancel()
            self._retry = None
        if self._runner:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        if self._connection:
            self._lost(1000)
        tasks = tuple(task for task in self._callbacks if task is not asyncio.current_task())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _send(self, frame, handshake=False):
        if not self._socket or self._socket.closed or (not handshake and not self.ready):
            raise HermesGlassesError('Not connected.', 'disconnected')
        try:  # serialise here: a frame that cannot be sent fails its caller, never the writer
            data = json.dumps(frame)
        except (TypeError, ValueError) as error:
            raise HermesGlassesError(f'Frame cannot be sent: {type(error).__name__}.') from None
        self._outbox.put_nowait(data)

    def _next_id(self):
        while True:
            self._sequence += 1
            id = f'q-{self._prefix}-{self._sequence}'
            if id not in self._queries and id not in self._interrupts:
                return id

    async def ask(self, text='', audio=None, image=None, speak=None, id=None, timeout=180):
        id = self._next_id() if id is None else id
        if not isinstance(id, str) or not id or id in self._queries or id in self._interrupts:
            raise HermesGlassesError('Query id must be a unique nonempty string.')
        future = asyncio.get_running_loop().create_future()
        pending = {'future': future, 'speak': speak, 'slash': not audio and text.strip().startswith('/')}
        self._queries[id] = pending
        if timeout:
            pending['deadline'] = asyncio.get_running_loop().call_later(
                timeout, self._finish, id, HermesGlassesError('Query timed out.', 'timeout'))
        try:
            frame = {'type': 'query', 'id': id, 'text': text}
            frame.update({k: v for k, v in (('audio', audio), ('image', image), ('speak', speak)) if v is not None})
            self._send(frame)
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            error = HermesGlassesError('Query aborted.', 'aborted')
            self._finish(id, error)
            if future.done() and not future.cancelled():
                future.exception()
            raise error from None
        except HermesGlassesError as error:
            self._finish(id, error)
            if future.done():
                future.exception()
            raise

    @staticmethod
    def _settle(future, result=None, error=None):
        if future is not None and not future.done():
            if error:
                future.set_exception(error)
            else:
                future.set_result(result)

    def _finish(self, id, error=None):
        pending = self._queries.get(id)
        if pending is None or ('result' not in pending and error is None):
            return
        del self._queries[id]
        for key in ('timer', 'deadline'):
            if pending.get(key):
                pending[key].cancel()
        self._settle(pending['future'], pending.get('result'), error)

    async def new_session(self):
        if self._reset is None:
            self._send({'type': 'new_session'})
            self._reset = asyncio.get_running_loop().create_future()
        return await asyncio.shield(self._reset)

    def interrupt(self, ask=None):
        id = self._next_id()
        self._send({'type': 'interrupt', 'id': id})
        self._interrupts.add(id)
        if ask is None:
            ask = next((id for id, q in self._queries.items() if 'result' not in q and not q['slash']), None)
        self._finish(ask, None if self._queries.get(ask, {}).get('result') else Interrupted())

    async def answer_approval(self, id, choice):
        if id in self._approvals:
            raise HermesGlassesError('Approval answer already pending.')
        self._send({'type': 'approval_response', 'id': id, 'choice': choice})
        future = self._approvals[id] = asyncio.get_running_loop().create_future()
        return await asyncio.shield(future)

    def send_photo(self, request, b64jpeg):
        self._send({'type': 'photo', 'request': request, 'data': b64jpeg, 'mime': 'image/jpeg'})

    def photo_error(self, request, msg):
        self._send({'type': 'photo_error', 'request': request, 'message': msg})

    async def _camera(self, frame, result, ws):
        try:
            data = await result if inspect.isawaitable(result) else result
            if not isinstance(data, str):
                raise ValueError()
            if ws is self._socket:
                self.send_photo(frame.get('request'), data)
        except Exception:
            if self.ready and ws is self._socket:
                self.photo_error(frame.get('request'), 'Camera unavailable.')

    def _receive(self, frame):
        kind, id = frame.get('type'), frame.get('turn')
        pending = self._queries.get(id) if isinstance(id, str) else None
        if kind == 'welcome':
            hello = {'type': 'hello', 'protocol': 1, 'device': self.device}
            if self.app is not None:
                hello['app'] = self.app
            self._send(hello, handshake=True)
        elif kind == 'hello_ack':
            self.ready, self.handshake, self._backoff = True, frame, 1
            self._settle(self._connection, frame)
            self._connection = None
            self._emit('connected', frame)
        elif kind == 'response':
            if isinstance(id, str) and id in self._interrupts:
                self._interrupts.remove(id)
            elif pending is not None and 'result' not in pending:
                pending['result'] = TurnResult(id, frame.get('text'), pending.get('transcript'))
                features = ((self.handshake or {}).get('server') or {}).get('features') or []
                if self.device.get('capabilities', {}).get('audio_out') and pending['speak'] is not False and 'audio_out' in features:
                    pending['timer'] = asyncio.get_running_loop().call_later(15, self._finish, id)
                else:
                    self._finish(id)
        elif kind == 'audio':
            query = self._queries.get(frame.get('ref')) if isinstance(frame.get('ref'), str) else None
            if query and 'result' in query:
                query['result'].audio = {'data': frame.get('data'), 'mime': frame.get('mime')}
                self._finish(frame['ref'])
            self._emit('audio', frame)
        elif kind in ('transcript', 'response_delta'):
            if kind == 'transcript' and pending is not None:
                pending['transcript'] = frame.get('text')
            self._emit('transcript' if kind == 'transcript' else 'delta', {**frame, 'id': id})
        elif kind == 'status':
            inferred = next(iter(self._queries)) if len(self._queries) == 1 else None
            self._emit('status', {**frame, 'id': id if id is not None else inferred})
        elif kind == 'error':
            message = str(frame.get('message') or 'Server error.')
            if self.token:
                message = message.replace(self.token, '[redacted]')
            error = HermesGlassesError(message)
            correlated = frame.get('turn', frame.get('id'))
            in_flight = len(self._queries)
            keys = list(self._queries) if correlated is None else [correlated] if isinstance(correlated, str) else []
            for key in keys:
                query = self._queries.get(key)
                if query is not None:
                    self._finish(key, None if 'result' in query else error)
            if frame.get('message') == 'New session failed.' or in_flight == 0:
                self._settle(self._reset, error=error)
                self._reset = None
            self._emit('error', {**frame, 'message': message})
        elif kind == 'session_reset':
            self._settle(self._reset, frame)
            self._reset = None
        elif kind == 'approval_result':
            if isinstance(frame.get('id'), str):
                self._settle(self._approvals.pop(frame['id'], None), frame)
        elif kind == 'push':
            if self.device.get('capabilities', {}).get('ack'):
                self._send({'type': 'ack', 'id': frame.get('id')})
            self._emit('push', frame)
        elif kind == 'capture_photo':
            if self.on_capture_photo:
                try:
                    result = self.on_capture_photo(frame.get('reason'))
                except Exception:
                    if self.ready:
                        self.photo_error(frame.get('request'), 'Camera unavailable.')
                else:
                    self._spawn(self._camera(frame, result, self._socket))
            self._emit(kind, frame)
        elif kind in ('approval_request', 'approval_closed', 'display', 'haptic'):
            self._emit(kind, frame)


class HermesGlassesHttp:
    def __init__(self, base_url, token):
        self.base_url, self.token = base_url.rstrip('/'), token

    async def _request(self, path, body=None, text_plain=False):
        headers = {'Authorization': 'Bearer ' + self.token,
                   'Accept': 'text/plain' if text_plain else 'application/json'}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request('GET' if body is None else 'POST', self.base_url + path,
                                           headers=headers, **({} if body is None else {'json': body})) as response:
                    if not 200 <= response.status < 300:
                        raise HermesGlassesError(f'HTTP {response.status}')
                    if text_plain:
                        return {'status': 'pending' if response.status == 202 else 'done', 'text': await response.text()}
                    return await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise HermesGlassesError('HTTP transport or response error.') from None

    async def turn(self, body=None, *, text_plain=False, **query):
        return await self._request('/turn', query if body is None else body, text_plain)

    async def pending(self, wait=25):
        from urllib.parse import urlencode
        return await self._request('/pending?' + urlencode({'wait': wait}))

    async def approval(self, id, choice):
        return await self._request('/approval', {'id': id, 'choice': choice})
