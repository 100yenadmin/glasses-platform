"""Small protocol v0/v1 frame builders. Unknown client extensions are tolerated."""
import json
from pathlib import Path
import re

try:
    _VERSION = re.search(r'^version:\s*[\"\']?([^\s\"\']+)',
                         Path(__file__).with_name('plugin.yaml').read_text(encoding='utf-8'), re.M).group(1)
except (OSError, ValueError, AttributeError):
    _VERSION = 'unknown'


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


def status(state, detail=None):
    frame = {'type': 'status', 'state': state}
    if detail:
        frame['detail'] = str(detail)[:200]
    return frame


def welcome():
    return {'type': 'welcome', 'protocol_max': 1}


def hello_ack(user, device):
    return {'type': 'hello_ack', 'protocol': 1, 'user': user, 'device': device,
            'server': {'plugin': 'glasses-platform', 'version': _VERSION,
                       'features': ['audio_in', 'audio_out', 'approval_buttons', 'ack', 'display', 'haptic']}}


def transcript(turn, text):
    return {'type': 'transcript', 'turn': turn, 'text': text}


def audio(ref, data_b64, mime):
    return {'type': 'audio', 'ref': ref, 'data': data_b64, 'mime': mime}


def approval_request(id, text, command, description, choices):
    return {'type': 'approval_request', 'id': id, 'text': text, 'command': command,
            'description': description, 'choices': choices}


def approval_result(id, ok, message, choice=None):
    return {'type': 'approval_result', 'id': id, 'ok': ok, 'message': message,
            **({'choice': choice} if choice is not None else {})}


def approval_closed(id, choice=None):
    return {'type': 'approval_closed', 'id': id, **({'choice': choice} if choice is not None else {})}


def display(text, seconds=None):
    return {'type': 'display', 'text': text, **({'seconds': seconds} if seconds is not None else {})}


def haptic(pattern):
    return {'type': 'haptic', 'pattern': pattern}


def session_reset():
    return {'type': 'session_reset'}


def pong():
    return {'type': 'pong'}


def error(message, turn=None):
    """v0 frames stay exactly `{type, message}`; a v1 error that answers one query names it (`turn`)."""
    return {'type': 'error', 'message': message, **({'turn': turn} if turn else {})}
