"""v0.1.1 regressions from the hackathon demo dry run (real Hermes 0.23.0 + simulator in v0 mode):
a new_session queued during gateway startup, Hermes' cron wrapper read aloud, and "remember that my ..." photos."""
from unittest.mock import AsyncMock

import pytest
from gateway.platforms.event import ProcessingOutcome
from glasses_plugin.capture import should_capture_photo
from glasses_plugin.devices import Connection, Identity


class FakeWS:
    def __init__(self):
        self.closed, self.frames = False, []

    async def send_json(self, frame):
        self.frames.append(frame)

    async def close(self, **kwargs):
        self.closed = True


async def device(adapter, name='ray', protocol=0):
    conn = Connection(FakeWS(), Identity('alice', name), protocol=protocol)
    conn.flushed = True
    await adapter.registry.add(conn)
    return conn


def kinds(conn):
    return [frame['type'] for frame in conn.ws.frames]


async def queued_then_replayed(adapter, event):
    """Hermes' startup-restore gate: the processing hooks fire around an event it only queues, then it replays
    the same event object later with the hooks again."""
    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    event._hermes_startup_restore_replay = True
    await adapter.on_processing_start(event)


@pytest.mark.asyncio
@pytest.mark.parametrize('confirm', [True, False])
async def test_new_session_queued_at_startup_is_acknowledged_once_when_it_runs(server, confirm):
    adapter, _ = server
    from tools import slash_confirm
    glasses = await device(adapter)
    adapter.handle_message = AsyncMock()
    await adapter._new_session(glasses)
    event = adapter.handle_message.call_args.args[0]
    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert glasses.ws.frames == [] and event.message_id in adapter._resets  # only queued: nothing to answer yet
    event._hermes_startup_restore_replay = True
    await adapter.on_processing_start(event)
    if confirm:
        async def handler(choice):
            return 'New session.'
        slash_confirm.register('session-alice', 'c1', 'new', handler)
        assert (await adapter.send_slash_confirm('alice', 'Confirm /new', 'prompt', 'session-alice', 'c1')).success
    else:  # approvals.destructive_slash_confirm off: Hermes sends the /new result text
        await adapter.send('alice', 'New session.', reply_to=event.message_id, metadata={'notify': True})
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert glasses.ws.frames == [{'type': 'session_reset'}] and not adapter._resets


@pytest.mark.asyncio
async def test_query_queued_at_startup_gets_one_reply_from_its_replay(server):
    adapter, _ = server
    glasses = await device(adapter)
    adapter.handle_message = AsyncMock()
    await adapter._process_query(glasses, {'type': 'query', 'text': 'hello', 'id': 'q1'})
    event = adapter.handle_message.call_args.args[0]
    await queued_then_replayed(adapter, event)
    await adapter.send('alice', '👁️ Looking at the image', metadata={'_interim_send': True})
    await adapter.send('alice', 'Hello there.', metadata={'notify': True})
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert glasses.ws.frames == [{'type': 'response', 'text': 'Hello there.', 'tts': False}]


WRAPPED = ('Cronjob Response: Call Sam\n(job_id: 09315b831a29)\n-------------\n\n'
           'Reminder: call Sam about gate 42.\n\n'
           'To stop or manage this job, send me a new message (e.g. "stop reminder Call Sam").')


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', [0, 1])
async def test_cron_wrapper_is_not_read_aloud(server, protocol):
    adapter, _ = server
    glasses = await device(adapter, protocol=protocol)
    await adapter.send('alice', WRAPPED, metadata={'job_id': '09315b831a29', 'notify': True})
    assert [frame['text'] for frame in glasses.ws.frames] == ['Reminder: call Sam about gate 42.']


@pytest.mark.asyncio
async def test_only_cron_deliveries_are_unwrapped(server):
    adapter, _ = server
    glasses = await device(adapter, protocol=1)
    await adapter.send('alice', WRAPPED, metadata={'notify': True})  # not from cron: the text is the message
    await adapter.send('alice', 'Plain cron text.', metadata={'job_id': 'j1'})
    assert [frame['text'] for frame in glasses.ws.frames] == [WRAPPED, 'Plain cron text.']


def test_that_as_a_conjunction_takes_no_photo():
    for text in ('Remember that my gate is 42.', 'I think that the bus left.', 'Tell her that we are late.',
                 'Is this it?', 'So that you know.'):
        assert not should_capture_photo(text, 0, 1000), text
    for text in ('What is that building?', 'How old is this bridge?', 'that one, please'):
        assert should_capture_photo(text, 0, 1000), text
