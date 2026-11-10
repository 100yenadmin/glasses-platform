"""A dangerous-command approval is said as a yes/no question, and a spoken yes or no answers it."""
import asyncio
from dataclasses import fields
from unittest.mock import AsyncMock

import pytest
from gateway.platforms.base import ExecApprovalPrompt
from glasses_plugin.adapter import GlassesAdapter, spoken_choice
from glasses_plugin.devices import parse_devices
from glasses_plugin.tests.test_routing_fixes import opened, until

ACTIONS = [('Allow once', 'once', 'primary'), ('Allow for session', 'session', ''),
           ('Always allow', 'always', ''), ('Deny', 'deny', 'danger')]


HAS_ID = 'request_id' in {f.name for f in fields(ExecApprovalPrompt)}  # Hermes 0.23 and later


def prompt(user='alice', request_id='r1', actions=ACTIONS, command='rm -rf build/'):
    extra = {'request_id': request_id} if HAS_ID else {}
    return ExecApprovalPrompt(chat_id=user, session_key=f'agent:main:glasses:dm:{user}', text='(card text)',
                              actions=actions, command=command, description='Deletes files.',
                              smart_denied=False, **extra)





@pytest.fixture
def hermes(server, monkeypatch):
    adapter, client = server
    adapter._tokens = parse_devices('alice:ray=tok-alice-1,alice:phone=tok-alice-2,bob:g2=tok-bob-1', '')
    adapter.handle_message = AsyncMock()
    calls, queue = [], []  # queue: Hermes' unresolved request ids for the session

    def resolve(session_key, choice, resolve_all=False, reason=None, request_id=None):
        calls.append((session_key, choice, request_id))
        if request_id in queue:
            queue.remove(request_id)
            return 1
        return 0
    resolve.queue = queue
    resolve.clarify = set()  # session keys with an open clarify question
    monkeypatch.setattr('tools.approval.resolve_gateway_approval', resolve)
    monkeypatch.setattr('tools.clarify_gateway.has_pending', lambda key: key in resolve.clarify)
    monkeypatch.setattr('tools.approval.list_gateway_approvals', lambda key: [{'request_id': r} for r in queue])
    return adapter, client, calls, resolve


async def ask_approval(adapter, resolve, request_id='r1', **kw):
    resolve.queue.append(request_id)  # Hermes queues the request, then asks the adapter to show it
    return await adapter._send_exec_approval_prompt(prompt(request_id=request_id, **kw))


async def say(ws, text, query_id='q1'):
    await ws.send_json({'type': 'query', 'id': query_id, 'text': text})


@pytest.mark.parametrize('text,choice', [
    ('Yes.', 'once'), ('yeah', 'once'), ('Hermes, go ahead!', 'once'), ('OK', 'once'), ('Allow it', 'once'),
    ('No', 'deny'), ("Don't do it.", 'deny'), ('don’t', 'deny'), ('Hey Hermes, stop', 'deny'), ('cancel', 'deny'),
    ('yes, and also delete the cache', None), ('always allow', None), ('what does it do?', None), ('', None)])
def test_only_a_whole_yes_or_no_counts(text, choice):
    assert spoken_choice(text) == choice


def test_the_adapter_renders_native_approvals():
    assert GlassesAdapter.supports_exec_approval_buttons()


@pytest.mark.asyncio
async def test_the_prompt_is_a_short_question_on_every_device_even_a_waiting_v0_one(hermes):
    adapter, client, calls, resolve = hermes
    ray = await opened(client, version=1)
    phone = await opened(client, 'tok-alice-2', version=0)
    await say(phone, 'clean the build folder')  # the v0 phone now waits for its answer
    await asyncio.sleep(0.05)
    result = await ask_approval(adapter, resolve)
    assert result.success
    pushed = await until(ray, 'push')
    assert pushed['origin'] == 'approval'
    assert pushed['text'].startswith('Hermes wants to run: rm -rf build/. Deletes files. Say yes to allow it once')
    assert '**' not in pushed['text'] and '⚠' not in pushed['text']
    assert (await until(phone, 'response'))['text'] == pushed['text']  # not held behind the phone's question
    await ray.close(); await phone.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('words,choice,reply', [('Yes, go ahead', None, None), ('yes', 'once', 'Okay, going ahead.'),
                                                ('No.', 'deny', "Okay, I won't run it.")])
async def test_a_spoken_yes_or_no_answers_that_request(hermes, words, choice, reply):
    adapter, client, calls, resolve = hermes
    ray = await opened(client, version=1)
    await ask_approval(adapter, resolve)
    await until(ray, 'push')
    await say(ray, words)
    if choice is None:  # not a whole yes: the agent hears it and the approval still waits
        await asyncio.sleep(0.1)
        assert calls == [] and adapter.handle_message.await_count == 1 and adapter._approvals['alice']
    else:
        frame = await until(ray, 'response')
        assert (frame['text'], frame['turn']) == (reply, 'q1')
        assert calls == [('agent:main:glasses:dm:alice', choice, 'r1')]
        assert adapter.handle_message.await_count == 0 and 'alice' not in adapter._approvals
    await ray.close()


@pytest.mark.asyncio
async def test_yes_goes_to_the_agent_when_nothing_waits(hermes):
    adapter, client, calls, resolve = hermes
    ray = await opened(client, version=1)
    await say(ray, 'yes', 'q0')
    await asyncio.sleep(0.1)
    assert calls == [] and adapter.handle_message.await_count == 1
    await ask_approval(adapter, resolve)
    await until(ray, 'push')
    resolve.queue.clear()  # answered by a typed /approve, or timed out
    await say(ray, 'yes')
    await asyncio.sleep(0.1)
    assert calls == [] and adapter.handle_message.await_count == 2 and 'alice' not in adapter._approvals
    await ray.close()


@pytest.mark.asyncio
async def test_a_stale_yes_never_approves_a_newer_request(hermes):
    adapter, client, calls, resolve = hermes
    ray = await opened(client, version=1)
    await ask_approval(adapter, resolve, 'r1')
    resolve.queue.remove('r1')  # r1 settled elsewhere
    await ask_approval(adapter, resolve, 'r2', command='git push --force')
    await say(ray, 'yes')  # only r2 is still waiting, and the device was asked about it last
    await until(ray, 'response')
    assert calls == [('agent:main:glasses:dm:alice', 'once', 'r2')]
    await ray.close()


@pytest.mark.asyncio
async def test_with_two_waiting_a_bare_yes_resolves_nothing(hermes):
    adapter, client, calls, resolve = hermes
    ray = await opened(client, version=1)
    await ask_approval(adapter, resolve, 'r1')
    await ask_approval(adapter, resolve, 'r2', command='git push --force')
    await say(ray, 'yes')
    frame = await until(ray, 'response')
    assert frame['text'].startswith('I was not sure which command') and calls == []
    assert adapter.handle_message.await_count == 0
    await ray.close()


@pytest.mark.asyncio
async def test_the_answer_to_a_clarify_question_is_never_taken_as_an_approval(hermes):
    adapter, client, calls, resolve = hermes
    ray = await opened(client, version=1)
    await ask_approval(adapter, resolve)
    resolve.clarify.add('agent:main:glasses:dm:alice')  # Hermes asked "Delete the backups too?" and waits
    await say(ray, '/status', 'q0')
    await say(ray, 'yes')
    await asyncio.sleep(0.1)
    assert calls == [] and adapter.handle_message.await_count == 2  # both reach Hermes; its clarify handler gets "yes"
    resolve.clarify.clear()  # the question was answered
    await say(ray, 'yes', 'q2')
    assert (await until(ray, 'response'))['turn'] == 'q2' and calls[0][2] == 'r1'
    await ray.close()


@pytest.mark.asyncio
async def test_an_answer_to_a_request_that_just_ended_never_moves_to_the_next(hermes):
    adapter, client, calls, resolve = hermes
    ray = await opened(client, version=1)
    await ask_approval(adapter, resolve, 'r1')
    await ask_approval(adapter, resolve, 'r2', command='git push --force')
    resolve.queue.remove('r1')  # r1 timed out just as the wearer said yes to it
    await say(ray, 'yes')
    assert (await until(ray, 'response'))['text'].startswith('I was not sure') and calls == []
    await ray.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('recorded', [False, True])
async def test_a_waiting_request_the_adapter_could_not_identify_keeps_a_yes_from_hermes(hermes, recorded):
    """Hermes routes a bare "yes" to /approve for its OLDEST request: it must not get one while an unknown request waits."""
    adapter, client, calls, resolve = hermes
    ray = await opened(client, version=1)
    if recorded:
        await ask_approval(adapter, resolve, 'r1')
        resolve.queue.append('r2')  # a second request whose prompt went out as Hermes' text fallback
    else:
        bare = ExecApprovalPrompt(**{f.name: getattr(prompt(), f.name) for f in fields(ExecApprovalPrompt)
                                     if f.name != 'request_id'})
        resolve.queue[:] = ['a', 'b']  # two parallel requests on Hermes 0.22.2: neither identifiable
        assert not (await adapter._send_exec_approval_prompt(bare)).success
    await say(ray, 'yes')
    assert (await until(ray, 'response'))['text'].startswith('I was not sure')
    assert calls == [] and adapter.handle_message.await_count == 0
    await ray.close()


@pytest.mark.asyncio
async def test_a_request_not_yet_shown_is_never_answered(hermes):
    from glasses_plugin.adapter import Approval
    adapter, client, calls, resolve = hermes
    ray = await opened(client, version=1)
    resolve.queue.append('r9')
    adapter._approvals['alice'] = [Approval('agent:main:glasses:dm:alice', 'r9', ['once', 'deny'], 0.0)]  # push pending
    adapter._approval_sessions['alice'] = {'agent:main:glasses:dm:alice'}
    await say(ray, 'yes')
    assert (await until(ray, 'response'))['text'].startswith('I was not sure') and calls == []
    await ray.close()


@pytest.mark.asyncio
async def test_without_a_request_id_the_one_new_request_is_used_or_hermes_text_is(hermes, monkeypatch):
    adapter, client, calls, resolve = hermes
    bare = ExecApprovalPrompt(**{f.name: getattr(prompt(), f.name) for f in fields(ExecApprovalPrompt)
                                 if f.name != 'request_id'})  # what Hermes 0.22.2 passes
    assert not (await adapter._send_exec_approval_prompt(bare)).success  # nothing queued: not identifiable
    resolve.queue[:] = ['old', 'new']
    assert not (await adapter._send_exec_approval_prompt(bare)).success  # two unknown: still ambiguous
    resolve.queue[:] = ['new']
    assert (await adapter._send_exec_approval_prompt(bare)).success
    assert adapter._approvals['alice'][0].request_id == 'new'


@pytest.mark.asyncio
async def test_another_users_yes_never_answers_it(hermes, monkeypatch):
    adapter, client, calls, resolve = hermes
    alice = 'agent:main:glasses:dm:alice'  # Hermes' queue is per session: bob's session has nothing waiting
    monkeypatch.setattr('tools.approval.list_gateway_approvals',
                        lambda key: [{'request_id': r} for r in resolve.queue] if key == alice else [])
    await ask_approval(adapter, resolve)
    bob = await opened(client, 'tok-bob-1', version=1)
    await say(bob, 'yes')
    await asyncio.sleep(0.1)
    assert calls == [] and adapter.handle_message.await_count == 1 and adapter._approvals['alice']
    await bob.close()


@pytest.mark.asyncio
async def test_yes_never_approves_more_than_offered(hermes):
    adapter, client, calls, resolve = hermes
    ray = await opened(client, version=1)
    await ask_approval(adapter, resolve, actions=[('Deny', 'deny', 'danger')])
    assert 'Say no to block it.' in (await until(ray, 'push'))['text']
    await say(ray, 'yes')  # kept here: Hermes would take a bare "yes" as /approve
    assert (await until(ray, 'response'))['text'] == 'This command can only be blocked. Say no to block it.'
    assert calls == [] and adapter.handle_message.await_count == 0
    await say(ray, 'no', 'q2')
    assert (await until(ray, 'response'))['turn'] == 'q2' and calls[0][1] == 'deny'
    await ray.close()


@pytest.mark.asyncio
async def test_try_can_answer_an_approval_while_the_turn_waits(hermes):
    from glasses_plugin.sim.glasses_sim import chat
    adapter, client, calls, resolve = hermes
    lines = iter(['delete the build folder', 'yes', ''])

    async def approval_soon():
        await asyncio.sleep(0.2)  # the turn is running (Hermes never answers it here): ask for approval now
        await ask_approval(adapter, resolve)

    def ask(_prompt):
        line = next(lines)
        if line.startswith('delete'):
            asyncio.run_coroutine_threadsafe(approval_soon(), loop)
        return line
    loop = asyncio.get_running_loop()
    url = str(client.make_url('/voice'))
    code = await asyncio.wait_for(chat(url, 'tok-alice-1', emit=lambda *a: None, ask=ask), 5)
    assert code == 0 and calls == [('agent:main:glasses:dm:alice', 'once', 'r1')]


@pytest.mark.asyncio
async def test_a_prompt_for_an_unknown_user_falls_back_to_hermes_text(hermes):
    adapter, *_ = hermes
    assert not (await adapter._send_exec_approval_prompt(prompt('mallory'))).success


def test_the_session_key_matches_the_one_hermes_checks(make_adapter):
    adapter = make_adapter()
    source = adapter.build_source(chat_id='alice', chat_name='alice', chat_type='dm', user_id='alice', user_name='alice')
    assert adapter._source_session_key(source) == 'agent:main:glasses:dm:alice'  # the key these tests use


@pytest.mark.asyncio
async def test_a_typed_slash_command_always_goes_to_hermes(hermes):
    adapter, client, calls, resolve = hermes
    ray = await opened(client, version=1)
    await ask_approval(adapter, resolve, 'r1')
    await ask_approval(adapter, resolve, 'r2')
    await say(ray, '/approve')  # the explicit command: Hermes' own semantics
    await asyncio.sleep(0.1)
    assert calls == [] and adapter.handle_message.await_count == 1
    await ray.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('word', ['confirm', 'y', 'always', 'approve session', 'reject', 'n', '\U0001f44d'])
async def test_every_hermes_approval_word_is_kept_when_unsure(hermes, word):
    adapter, client, calls, resolve = hermes
    ray = await opened(client, version=1)
    await ask_approval(adapter, resolve, 'r1')
    await ask_approval(adapter, resolve, 'r2')
    await say(ray, word)
    assert (await until(ray, 'response'))['text'].startswith('I was not sure')
    assert calls == [] and adapter.handle_message.await_count == 0
    await ray.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('word,choice,reply', [
    ('confirm', 'once', 'Okay, going ahead.'), ('\U0001f44e', 'deny', "Okay, I won't run it."),
    ('always', None, 'By voice I can only allow a command once. Say yes or no.'),
    ('approve session', None, 'By voice I can only allow a command once. Say yes or no.')])
async def test_hermes_words_with_one_request_waiting(hermes, word, choice, reply):
    adapter, client, calls, resolve = hermes
    ray = await opened(client, version=1)
    await ask_approval(adapter, resolve, 'r1')
    await say(ray, word)
    assert (await until(ray, 'response'))['text'] == reply
    assert calls == ([('agent:main:glasses:dm:alice', choice, 'r1')] if choice else [])
    await ray.close()
