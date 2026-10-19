import pytest
from glasses_plugin import protocol as p


@pytest.mark.parametrize('raw', ['{', 'null', '[]', '42', '{}', '{"type":null}', '{"type":1}', None])
def test_invalid(raw):
    assert p.parse_client_frame(raw) is None


def test_versions():
    assert p.parse_client_frame('{"type":"new-extension","x":1}')['x'] == 1
    assert p.response(0, 'hi') == {'type': 'response', 'text': 'hi', 'tts': False}
    assert p.response(1, 'hi', 't') == {'type': 'response', 'text': 'hi', 'tts': False, 'turn': 't', 'final': True}
    assert p.capture_photo(0, 'r', 'keyword') == {'type': 'capture_photo'}
    assert p.capture_photo(1, 'r', 'tool')['request'] == 'r'
    assert p.push(0, 'hi', 'cron', 'i') == p.response(0, 'hi')
    assert p.push(1, 'hi', 'cron', 'i')['origin'] == 'cron'
    assert p.response_delta('t', 'a') == {'type': 'response_delta', 'turn': 't', 'text': 'a'}
    assert p.status('idle') == {'type': 'status', 'state': 'idle'}
