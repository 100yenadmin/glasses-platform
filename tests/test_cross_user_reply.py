"""Query ids are chosen by clients: one user's id must never route another user's inline reply."""
import pytest

from glasses_plugin.devices import Connection, Identity

pytestmark = pytest.mark.asyncio


class Socket:
    closed = False

    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(frame)

    async def close(self, code=1000, message=b''):
        self.closed = True


async def test_inline_reply_never_goes_to_another_users_query_with_the_same_id(make_adapter):
    adapter = make_adapter(devices='alice:ray=tok-a,bob:ray=tok-b')
    alice = Connection(Socket(), Identity('alice', 'ray'), protocol=1, flushed=True)
    await adapter.registry.add(alice)
    adapter._queries['q1'] = alice  # alice's in-flight query uses the id "q1"
    await adapter.send('bob', "Bob's /status answer", reply_to='q1', metadata={'notify': True})
    assert not any("Bob's" in str(frame.get('text')) for frame in alice.ws.frames)


async def test_another_users_reply_never_acknowledges_my_pending_new_session(make_adapter):
    import time
    adapter = make_adapter(devices='alice:ray=tok-a,bob:ray=tok-b')
    alice = Connection(Socket(), Identity('alice', 'ray'), protocol=1, flushed=True)
    await adapter.registry.add(alice)
    adapter._queries['q1'] = alice
    adapter._resets['q1'] = (alice, time.monotonic())  # alice's new_session "q1" waits for Hermes
    await adapter.send('bob', 'Session reset.', reply_to='q1', metadata={'notify': True})
    assert not any(frame.get('type') == 'session_reset' for frame in alice.ws.frames)
    assert 'q1' in adapter._resets
