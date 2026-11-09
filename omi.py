"""Omi's live transcript webhook, carried by the existing short-lived HTTP connection."""
import re
from time import monotonic
from uuid import uuid4

from aiohttp import web
from gateway.platforms._shared import extra_or_secret

from .devices import Connection, authenticate
from .http_api import HttpSocket, _finish, _is_push, _reply, _requeue, _send, _terminal, _wait_for

WAKE = re.compile(r'^[\W_]*(?:(?:okay|ok)[\W_]+)?(?:hey[\W_]+)?hermes\b[\W_]*', re.IGNORECASE)
FOLLOWUP_SECONDS = 8
MESSAGE_LIMIT = 1000


class OmiApi:
    def __init__(self, adapter):
        self.adapter = adapter
        self.omi_wait = float(extra_or_secret(adapter.config.extra, 'omi_wait', 'GLASSES_OMI_WAIT', 20))
        self.sessions = {}  # (authenticated identity, session_id) -> seen segments and wake deadline

    def _questions(self, identity, session_id, segments):
        state = self.sessions.setdefault((identity, session_id), {'seen': set(), 'until': 0})
        now = monotonic()
        if state['until'] < now:
            state['until'] = 0
        user_only = any(segment.get('is_user') is True for segment in segments)
        questions, words = [], []
        for segment in segments:
            text = segment['text']
            key = (segment.get('start'), text)
            if key in state['seen']:
                continue
            state['seen'].add(key)
            if user_only and segment.get('is_user') is not True:
                continue
            wake = WAKE.match(text)
            if wake:
                if words:
                    questions.append(' '.join(words))
                    words = []
                state['until'] = now + FOLLOWUP_SECONDS
                text = text[wake.end():]
            if state['until'] and text.strip():
                words.append(text.strip())
        if words:
            questions.append(' '.join(words))
            state['until'] = 0
        return questions

    async def _segments(self, request):
        try:
            segments = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text='{"error":"malformed JSON"}', content_type='application/json')
        if not isinstance(segments, list) or any(
                not isinstance(segment, dict) or not isinstance(segment.get('text'), str)
                or not isinstance(segment.get('start', 0), (int, float)) for segment in segments):
            raise web.HTTPBadRequest(text='{"error":"expected transcript segments"}', content_type='application/json')
        return segments

    @staticmethod
    def _answer(frames, turn_ids):
        # Keep arrival order (queued pushes precede this turn), dropping the oldest characters first.
        parts, spans, length = [], [], 0
        for frame in frames:
            if not (_is_push(frame) or frame.get('type') == 'response' and frame.get('turn') in turn_ids
                    or frame.get('type') == 'error'):
                continue
            text = frame.get('text', frame.get('message', ''))
            if not isinstance(text, str) or not text:
                continue
            length += 2 if parts else 0
            spans.append((frame, length))
            parts.append(text)
            length += len(text)
        message = '\n\n'.join(parts)[-MESSAGE_LIMIT:]
        # A partially clipped push is still queued: _send only acknowledges complete shown frames.
        shown = [frame for frame, start in spans if start >= length - MESSAGE_LIMIT]
        return web.json_response({'message': message} if message else {}), lambda frame: frame in shown

    async def transcript(self, request):
        identity = authenticate(request.match_info['token'], self.adapter._tokens)
        if identity is None:
            raise web.HTTPUnauthorized(text='{"error":"unauthorized"}', content_type='application/json')
        segments = await self._segments(request)
        questions = self._questions(identity, request.query.get('session_id', ''), segments)
        conn = Connection(HttpSocket(), identity, protocol=1, capabilities={},
                          slot=f'{identity.device}#omi-{uuid4().hex}')
        turns = [(uuid4().hex, text) for text in questions]
        conn.asking.update((turn_id, monotonic()) for turn_id, _ in turns)
        turn_ids = {turn_id for turn_id, _ in turns}
        reply = None
        delivery_id = uuid4().hex
        try:
            await self.adapter.registry.add(conn)
            await self.adapter._flush_outbox(conn)
            start = len(conn.ws.frames)
            for turn_id, text in turns:
                self.adapter._spawn(self.adapter._process_query(conn, {'type': 'query', 'id': turn_id, 'text': text}))
            if turns:
                await _wait_for(conn.ws, lambda frames: all(
                    _terminal(frames, turn_id, start) is not None for turn_id in turn_ids), self.omi_wait)
            frames = await _finish(self.adapter, conn)
            reply = '\n\n'.join(_reply(frames, turn_id) or '' for turn_id, _ in turns).strip()
            response, shown = self._answer(frames, turn_ids)
        except BaseException:
            frames = await _finish(self.adapter, conn)
            reply = '\n\n'.join(_reply(frames, turn_id) or '' for turn_id, _ in turns).strip()
            _requeue(self.adapter, conn, reply, delivery_id)
            raise
        return await _send(request, self.adapter, conn, response, shown, reply, delivery_id)


def register_routes(adapter, app):
    api = OmiApi(adapter)
    app.router.add_post('/omi/{token}/transcript', api.transcript)
