"""Small protocol v0/v1 frame builders. Unknown client extensions are tolerated."""
import json


def parse_client_frame(raw):
    try:
        frame = json.loads(raw)
        if isinstance(frame, dict) and isinstance(frame.get('type'), str):
            return frame
    except (ValueError, TypeError, RecursionError):
        pass
    return None


def response(version, text, turn=None):
    frame = {'type': 'response', 'text': text, 'tts': False}
    if version == 1:
        frame.update(turn=turn, final=True)
    return frame


def response_delta(turn, text):
    return {'type': 'response_delta', 'turn': turn, 'text': text}


def push(version, text, origin, message_id):
    if version == 0:
        return response(0, text)
    return {'type': 'push', 'id': message_id, 'origin': origin, 'text': text}


def capture_photo(version, request, reason):
    frame = {'type': 'capture_photo'}
    if version == 1:
        frame.update(request=request, reason=reason)
    return frame


def status(state):
    return {'type': 'status', 'state': state}


def welcome():
    return {'type': 'welcome', 'protocol_max': 1}


def hello_ack(user, device):
    return {'type': 'hello_ack', 'protocol': 1, 'user': user, 'device': device,
            'server': {'plugin': 'glasses-platform', 'version': '0.1.0'}}


def session_reset():
    return {'type': 'session_reset'}


def pong():
    return {'type': 'pong'}


def error(message):
    return {'type': 'error', 'message': message}
