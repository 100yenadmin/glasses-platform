"""Session-scoped camera tool; socket operations stay on the gateway loop."""
import base64

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
