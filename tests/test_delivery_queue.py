"""Queue and out-of-band delivery regressions (cross-model review C3–C6), on fake sockets for determinism."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from gateway.platforms.event import ProcessingOutcome
from glasses_plugin.adapter import Turn
from glasses_plugin.devices import Connection, Identity


class FakeWS:
    def __init__(self, gate=None, close_after=None):
        self.closed, self.frames, self.gate, self.close_after = False, [], gate, close_after

    async def send_json(self, frame):
        if self.gate is not None and not self.frames:
            await self.gate.wait()
        self.frames.append(frame)
        if self.close_after is not None and len(self.frames) >= self.close_after:
            self.closed = True

    async def close(self, **kwargs):
        self.closed = True


async def device(adapter, name, ws=None, protocol=1, flushed=False):
    conn = Connection(ws or FakeWS(), Identity('alice', name), protocol=protocol)
    conn.flushed = flushed
    await adapter.registry.add(conn)
    return conn


def texts(conn):
    return [frame['text'] for frame in conn.ws.frames]


def turn_event(message_id, user='alice'):
    return type('Event', (), {'source': type('Source', (), {'chat_id': user})(), 'message_id': message_id})()


@pytest.mark.asyncio
async def test_reply_for_offline_asker_goes_out_of_band_not_as_their_turn(server):
    adapter, _ = server
    glasses = await device(adapter, 'ray', flushed=True)
    phone = await device(adapter, 'phone', flushed=True)
    adapter._active_turn['alice'] = Turn(glasses, 't1', started=True)
    glasses.ws.closed = True
    preview = await adapter.send('alice', 'The answ', metadata={'expect_edits': True})
    await adapter.edit_message('alice', preview.message_id, 'The answer.')
    assert phone.ws.frames == []  # no deltas or status for a turn the phone did not ask
    await adapter.on_processing_complete(turn_event('t1'), ProcessingOutcome.SUCCESS)
    assert [(f['type'], f['origin'], f['text']) for f in phone.ws.frames] == [('push', 'reply', 'The answer.')]
    assert adapter.outbox.drain('alice') == []


@pytest.mark.asyncio
async def test_queued_pushes_keep_order_against_a_concurrent_push(server):
    adapter, _ = server
    adapter.outbox.append('alice', 'old1')
    adapter.outbox.append('alice', 'old2')
    gate = asyncio.Event()
    conn = await device(adapter, 'ray', FakeWS(gate))
    flush = asyncio.create_task(adapter._flush_outbox(conn))
    await asyncio.sleep(0.01)  # the flush is now blocked inside its first write
    push = asyncio.create_task(adapter.send('alice', 'new', metadata={'origin': 'cron'}))
    await asyncio.sleep(0.01)
    gate.set()
    await flush
    await push
    assert texts(conn) == ['old1', 'old2', 'new']


@pytest.mark.asyncio
async def test_every_connected_device_gets_a_push_queued_before_its_protocol_settled(server):
    adapter, _ = server
    glasses = await device(adapter, 'ray', protocol=0)
    phone = await device(adapter, 'phone', protocol=0)
    await adapter.send('alice', 'hi', metadata={'origin': 'cron'})
    await adapter._flush_outbox(glasses)
    await adapter._flush_outbox(phone)
    assert texts(glasses) == ['hi'] and texts(phone) == ['hi']


@pytest.mark.asyncio
async def test_device_leaving_before_its_flush_keeps_its_pushes(server):
    adapter, _ = server
    conn = await device(adapter, 'ray', protocol=0)
    await adapter.send('alice', 'hi', metadata={'origin': 'cron'})
    adapter._release(conn)
    assert [item[1] for item in adapter.outbox.drain('alice')] == ['hi']


@pytest.mark.asyncio
async def test_flush_that_hits_a_closed_socket_requeues_the_rest_in_order(server):
    adapter, _ = server
    for text in ('one', 'two', 'three'):
        adapter.outbox.append('alice', text)
    conn = await device(adapter, 'ray', FakeWS(close_after=1))
    await adapter._flush_outbox(conn)
    assert texts(conn) == ['one']
    assert [item[1] for item in adapter.outbox.drain('alice')] == ['two', 'three']


@pytest.mark.asyncio
async def test_cancelled_flush_requeues_the_unsent_batch(server):
    adapter, _ = server
    adapter.outbox.append('alice', 'one')
    adapter.outbox.append('alice', 'two')
    conn = await device(adapter, 'ray', FakeWS(asyncio.Event()))  # the first write never completes
    flush = asyncio.create_task(adapter._flush_outbox(conn))
    await asyncio.sleep(0.01)
    flush.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flush
    assert [item[1] for item in adapter.outbox.drain('alice')] == ['one', 'two']


def started(adapter, message_id, conn):
    event = turn_event(message_id)
    event._glasses_connection = conn
    adapter._queries[message_id] = conn
    return event


def replies(conn):
    return [(f.get('turn'), f['text']) for f in conn.ws.frames if f['type'] == 'response']


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_queued_follow_up_chain_answers_each_query_on_its_own_device(server, streaming):
    adapter, _ = server
    glasses = await device(adapter, 'ray', flushed=True)
    phone = await device(adapter, 'phone', flushed=True)
    a, b = started(adapter, 'A', glasses), started(adapter, 'B', phone)
    await adapter.on_processing_start(a)
    if streaming:
        preview = await adapter.send('alice', 'Answer', metadata={'expect_edits': True})
        await adapter.edit_message('alice', preview.message_id, 'Answer A.', finalize=True)
    else:  # Hermes' queued-lane first response, anchored to the opener
        await adapter.send('alice', 'Answer A.', reply_to='A', metadata={'notify': True})
    await adapter.on_processing_start(b)  # in-band follow-up (gateway/run_turn.py)
    assert replies(glasses) == [('A', 'Answer A.')]
    await adapter.on_processing_complete(b, ProcessingOutcome.SUCCESS)  # fires before B's final send
    await adapter.send('alice', 'Answer B.', reply_to='A', metadata={'notify': True})  # terminal reply, opener anchor
    await adapter.on_processing_complete(a, ProcessingOutcome.SUCCESS)
    assert replies(glasses) == [('A', 'Answer A.')] and replies(phone) == [('B', 'Answer B.')]
    assert not [f for f in phone.ws.frames + glasses.ws.frames if f['type'] == 'push']
    assert 'alice' not in adapter._active_turn


@pytest.mark.asyncio
async def test_answer_delivered_as_commentary_is_still_the_reply(server):
    adapter, _ = server
    glasses = await device(adapter, 'ray', flushed=True)
    a = started(adapter, 'A', glasses)
    await adapter.on_processing_start(a)
    await adapter.send('alice', 'The sign says 42.', metadata={'_interim_send': True})
    await adapter.on_processing_complete(a, ProcessingOutcome.SUCCESS)
    assert replies(glasses) == [('A', 'The sign says 42.')]


@pytest.mark.asyncio
async def test_inline_command_reply_goes_to_its_own_query_not_the_running_turn(server):
    adapter, _ = server
    glasses = await device(adapter, 'ray', flushed=True)
    phone = await device(adapter, 'phone', flushed=True)
    a = started(adapter, 'A', glasses)
    await adapter.on_processing_start(a)
    await adapter.send('alice', 'Answer A.', metadata={'notify': True})
    adapter._queries['STOP'] = phone  # a /stop sent from the phone, answered inline by base.py
    await adapter.send('alice', 'Stopped.', reply_to='STOP', metadata={'notify': True})
    assert replies(phone) == [('STOP', 'Stopped.')]
    await adapter.on_processing_complete(a, ProcessingOutcome.SUCCESS)
    assert replies(glasses) == [('A', 'Answer A.')]


@pytest.mark.asyncio
async def test_pending_copies_merge_without_reorder_or_replay(server):
    adapter, _ = server
    glasses = await device(adapter, 'ray')
    await adapter.send('alice', 'ONE', metadata={'origin': 'cron'})
    phone = await device(adapter, 'phone')
    await adapter.send('alice', 'TWO', metadata={'origin': 'cron'})
    adapter._release(phone)  # TWO was delivered nowhere yet: it goes back to the outbox
    await adapter._flush_outbox(glasses)
    assert texts(glasses) == ['ONE', 'TWO']
    assert adapter.outbox.drain('alice') == []


@pytest.mark.asyncio
async def test_pending_copy_already_delivered_elsewhere_is_not_replayed(server):
    adapter, _ = server
    glasses = await device(adapter, 'ray')
    phone = await device(adapter, 'phone', flushed=True)
    await adapter.send('alice', 'TWO', metadata={'origin': 'cron'})
    assert texts(phone) == ['TWO']
    adapter._release(glasses)
    assert adapter.outbox.drain('alice') == []


class FlakyWS(FakeWS):
    async def send_json(self, frame):
        if not getattr(self, 'failed', False):
            self.failed = True
            raise ConnectionError('transient')
        await super().send_json(frame)


@pytest.mark.asyncio
async def test_failed_flush_is_retried_before_newer_pushes(server):
    adapter, _ = server
    adapter.outbox.append('alice', 'OLD')
    conn = await device(adapter, 'ray', FlakyWS())
    await adapter._flush_outbox(conn)
    assert texts(conn) == [] and not conn.flushed
    await adapter.send('alice', 'NEW', metadata={'origin': 'cron'})
    assert texts(conn) == []  # held, not overtaking OLD
    await adapter._flush_outbox(conn)  # the device's next frame
    assert texts(conn) == ['OLD', 'NEW']


@pytest.mark.asyncio
async def test_media_sent_with_the_reply_is_a_note_not_the_answer(server):
    adapter, _ = server
    glasses = await device(adapter, 'ray', flushed=True)
    a = started(adapter, 'A', glasses)
    await adapter.on_processing_start(a)
    await adapter.send('alice', 'Here is the map.', metadata={'notify': True})
    # base.py _deliver_attachments: the default send_image falls back to sending the URL as text.
    await adapter.send_image(chat_id='alice', image_url='https://example.test/map.png', metadata={'notify': True})
    await adapter.on_processing_complete(a, ProcessingOutcome.SUCCESS)
    assert replies(glasses) == [('A', 'Here is the map.\nhttps://example.test/map.png')]


async def arrive(adapter, conn, query_id):
    """A query frame reaching the adapter while a chain runs (Hermes' busy handling is stubbed)."""
    adapter.handle_message = AsyncMock()
    await adapter._process_query(conn, {'type': 'query', 'text': 'what is 2 plus 2', 'id': query_id})


@pytest.mark.asyncio
async def test_query_folded_into_the_running_turn_gets_the_reply_on_its_device(server):
    adapter, _ = server
    glasses = await device(adapter, 'ray', flushed=True)
    phone = await device(adapter, 'phone', flushed=True)
    a = started(adapter, 'A', glasses)
    await adapter.on_processing_start(a)
    await arrive(adapter, phone, 'B')  # Hermes' interrupt folds it into A's run: no hooks, not queued
    preview = await adapter.send('alice', '4', metadata={'expect_edits': True})
    await adapter.edit_message('alice', preview.message_id, '4', finalize=True)
    await adapter.on_processing_complete(a, ProcessingOutcome.SUCCESS)
    assert replies(phone) == [('B', '4')] and replies(glasses) == []
    assert glasses.ws.frames[-1] == {'type': 'status', 'state': 'idle'}


@pytest.mark.asyncio
async def test_query_queued_for_later_is_not_given_the_running_turns_reply(server):
    adapter, _ = server
    glasses = await device(adapter, 'ray', flushed=True)
    phone = await device(adapter, 'phone', flushed=True)
    a = started(adapter, 'A', glasses)
    await adapter.on_processing_start(a)
    await arrive(adapter, phone, 'B')
    adapter._pending_messages['session'] = turn_event('B')  # Hermes runs it after A
    await adapter.send('alice', 'Answer A.', metadata={'notify': True})
    await adapter.on_processing_complete(a, ProcessingOutcome.SUCCESS)
    assert replies(glasses) == [('A', 'Answer A.')] and replies(phone) == []


@pytest.mark.asyncio
async def test_inline_answered_command_is_not_treated_as_folded(server):
    adapter, _ = server
    glasses = await device(adapter, 'ray', flushed=True)
    a = started(adapter, 'A', glasses)
    await adapter.on_processing_start(a)
    await arrive(adapter, glasses, 'STATUS')
    await adapter.send('alice', 'Working on it.', reply_to='STATUS', metadata={'notify': True})
    await adapter.send('alice', 'Answer A.', metadata={'notify': True})
    await adapter.on_processing_complete(a, ProcessingOutcome.SUCCESS)
    assert replies(glasses) == [('STATUS', 'Working on it.'), ('A', 'Answer A.')]


@pytest.mark.asyncio
async def test_superseded_querys_late_stream_write_does_not_replace_the_next_answer(server):
    adapter, _ = server
    glasses = await device(adapter, 'ray', flushed=True)
    a, b = started(adapter, 'A', glasses), started(adapter, 'B', glasses)
    await adapter.on_processing_start(a)
    await adapter.on_processing_start(b)  # A was interrupted before its stream drained
    await adapter.send('alice', 'Correct answer B.', reply_to='B', metadata={'notify': True, 'reply_to_message_id': 'B'})
    # Hermes drains A's interrupted stream during the outer cleanup (gateway/run_turn.py).
    await adapter.send('alice', 'Partial A', reply_to='A', metadata={'notify': True, 'reply_to_message_id': 'A'})
    await adapter.on_processing_complete(b, ProcessingOutcome.SUCCESS)
    await adapter.on_processing_complete(a, ProcessingOutcome.SUCCESS)
    assert replies(glasses) == [('B', 'Correct answer B.')]


@pytest.mark.asyncio
@pytest.mark.parametrize('confirm', [True, False])
async def test_new_session_while_a_turn_runs_resets_and_acknowledges(server, confirm):
    adapter, _ = server
    from tools import slash_confirm
    glasses = await device(adapter, 'ray', flushed=True)
    phone = await device(adapter, 'phone', flushed=True)
    a = started(adapter, 'A', glasses)
    await adapter.on_processing_start(a)
    adapter.handle_message = AsyncMock()
    await adapter._new_session(phone)  # Hermes dispatches the busy /new inline, without processing hooks
    reset_id = next(iter(adapter._resets))
    choices = []
    if confirm:
        async def handler(choice):
            choices.append(choice)
            return 'New session.'
        slash_confirm.register('session-alice', 'c9', 'new', handler)
        assert (await adapter.send_slash_confirm('alice', 'Confirm /new', 'prompt', 'session-alice', 'c9')).success
        assert choices == ['once']
    else:  # approvals.destructive_slash_confirm off: the inline reply is the /new result text
        await adapter.send('alice', 'New session.', reply_to=reset_id, metadata={'notify': True})
    assert phone.ws.frames == [{'type': 'session_reset'}] and not adapter._resets
    await adapter.send('alice', 'Answer A.', metadata={'notify': True})
    await adapter.on_processing_complete(a, ProcessingOutcome.SUCCESS)
    assert replies(glasses) == [('A', 'Answer A.')]
