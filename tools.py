"""Session-scoped camera tool; socket operations stay on the gateway loop."""
import base64
import math

from gateway.session_context import get_session_env
from . import adapter

DESCRIPTION = ("Take a photo with the user's smart glasses camera right now and look at it. "
               'Use when the user asks about something they are looking at.')
SCHEMA = {'name': 'glasses_capture_photo', 'description': DESCRIPTION,
          'parameters': {'type': 'object', 'properties': {'reason': {'type': 'string'}}, 'required': []}}


def available():
    return adapter.ACTIVE is not None


async def capture_photo(args):
    active = adapter.ACTIVE
    if get_session_env('HERMES_SESSION_PLATFORM') != 'glasses' or active is None:
        return 'No glasses session: this tool only works in a conversation from smart glasses.'
    user = get_session_env('HERMES_SESSION_CHAT_ID')
    result = await active.capture_for_session(user, 'tool')
    if result is None:
        return 'The glasses did not return a photo.'
    data, path = result
    return {'_multimodal': True,
            'content': [{'type': 'text', 'text': "Photo just captured from the user's glasses."},
                        {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(data).decode()}}],
            'text_summary': f'Photo cached at {path}. Use vision_analyze with this path if needed.',
            'meta': {'size_bytes': len(data), 'native_vision': True}}


SHOW_SCHEMA = {'name': 'glasses_show_text', 'description': "Show text on the user's glasses display.",
               'parameters': {'type': 'object', 'properties': {'text': {'type': 'string', 'maxLength': 500},
                              'seconds': {'type': 'number', 'exclusiveMinimum': 0}}, 'required': ['text']}}
BUZZ_SCHEMA = {'name': 'glasses_buzz', 'description': "Buzz the user's glasses.",
               'parameters': {'type': 'object', 'properties': {
                   'pattern': {'type': 'string', 'enum': ['tap', 'double', 'long'], 'default': 'tap'}}, 'required': []}}


async def _device_action(kind, payload):
    active = adapter.ACTIVE
    if get_session_env('HERMES_SESSION_PLATFORM') != 'glasses' or active is None:
        return 'No glasses session: this tool only works in a conversation from smart glasses.'
    count = await active.device_action(get_session_env('HERMES_SESSION_CHAT_ID'), kind, payload)
    return f'Sent to {count} device(s).'


async def show_text(args):
    text, seconds = args.get('text'), args.get('seconds')
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        return 'Text must contain 1–500 characters.'
    if seconds is not None and (type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0):
        return 'Seconds must be a positive number.'
    return await _device_action('display', {'text': text, **({'seconds': seconds} if seconds is not None else {})})


async def buzz(args):
    pattern = args.get('pattern', 'tap')
    if pattern not in ('tap', 'double', 'long'):
        return 'Pattern must be tap, double, or long.'
    return await _device_action('haptic', {'pattern': pattern})
