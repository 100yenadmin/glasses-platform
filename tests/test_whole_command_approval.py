"""An approval a device answers always showed the whole command: a clipped one can only be blocked from a device."""
import asyncio

import pytest
from glasses_plugin.adapter import WHOLE_COMMAND_LIMIT, approval_prompt_text
from glasses_plugin.tests.test_approval_buttons import approvals  # noqa: F401 (fixture)
from glasses_plugin.tests.test_routing_fixes import opened, until
from glasses_plugin.tests.test_spoken_approval import ask_approval, hermes, say  # noqa: F401 (fixture)
from glasses_plugin.tests.test_voice import device

LONG = 'python3 -c "' + 'x' * WHOLE_COMMAND_LIMIT + '"; rm -rf ~/'  # the dangerous part is past the limit
EDGE = 'echo ' + 'y' * (WHOLE_COMMAND_LIMIT - 5)  # exactly the limit: shown whole
CHOICES = ['once', 'session', 'always', 'deny']


def test_a_command_up_to_the_limit_is_said_whole_and_verbatim():
    text, offered, whole = approval_prompt_text('ls  -la\nrm -rf build/', 'Deletes files', CHOICES)
    assert whole and offered == CHOICES
    assert text.startswith('Hermes wants to run: ls  -la\nrm -rf build/. Deletes files. Say yes to allow it once')
    text, _, whole = approval_prompt_text(EDGE, '', CHOICES)
    assert whole and EDGE in text


def test_a_longer_command_is_clipped_and_offers_deny_only():
    text, offered, whole = approval_prompt_text(LONG, 'Runs code', CHOICES)
    assert not whole and offered == ['deny']
    assert 'rm -rf' not in text and 'Say yes' not in text
    assert text.endswith('Runs code. Say no to block it. Use /approve to allow it after reading the full command.')
    assert approval_prompt_text(LONG, '', ['deny'])[1] == ['deny']


@pytest.mark.asyncio
@pytest.mark.parametrize('words', ['yes', 'ok', 'sure', 'approve', 'always'])
async def test_a_spoken_yes_never_answers_a_clipped_command(hermes, words):
    adapter, client, calls, resolve = hermes
    ray = await opened(client, version=1)
    await ask_approval(adapter, resolve, command=LONG)
    pushed = await until(ray, 'push')
    assert 'long command that starts' in pushed['text'] and 'Say yes' not in pushed['text']
    await say(ray, words)
    frame = await until(ray, 'response')
    assert frame['text'].startswith('That command was too long to read out whole, so I can only block it by voice.')
    assert calls == [] and resolve.queue == ['r1'] and adapter._approvals['alice']
    assert adapter.handle_message.await_count == 0  # Hermes never hears the bare yes either
    await say(ray, 'no', 'q2')
    assert (await until(ray, 'response'))['text'] == "Okay, I won't run it."
    assert calls == [('agent:main:glasses:dm:alice', 'deny', 'r1')]
    await ray.close()


@pytest.mark.asyncio
async def test_a_short_command_shown_whole_is_still_approved_by_voice(hermes):
    adapter, client, calls, resolve = hermes
    ray = await opened(client, version=1)
    await ask_approval(adapter, resolve, command=EDGE)
    assert EDGE in (await until(ray, 'push'))['text']
    await say(ray, 'yes')
    assert (await until(ray, 'response'))['text'] == 'Okay, going ahead.'
    assert calls == [('agent:main:glasses:dm:alice', 'once', 'r1')]
    await ray.close()


@pytest.mark.asyncio
async def test_a_button_card_for_a_clipped_command_offers_deny_only(approvals):
    adapter, queues, calls, ask = approvals
    button = await device(adapter, buttons=True)
    approval = await ask(command=LONG)
    request = button.ws.frames[0]
    assert request['choices'] == ['deny'] and request['command'] == LONG
    for choice in ('once', 'session', 'always'):
        await adapter._button_approval(button, {'id': 'r1', 'choice': choice})
        result = button.ws.frames[-1]
        assert result['type'] == 'approval_result' and result['ok'] is False
    assert calls == [] and adapter._approvals['alice'] == [approval]
    await adapter._button_approval(button, {'id': 'r1', 'choice': 'deny'})
    assert button.ws.frames[-1]['ok'] is True
    assert calls == [(approval.session_key, 'deny', 'r1')]


@pytest.mark.asyncio
async def test_answers_resolve_through_hermes_card_resolver_when_it_has_one(approvals, monkeypatch):
    adapter, queues, calls, ask = approvals
    card, restricted = [], []
    def resolve_card(key, choice, request_id):
        card.append((key, choice, request_id))
        queues[key].remove(request_id)
        return 1
    monkeypatch.setattr('tools.approval.resolve_gateway_approval_card', resolve_card, raising=False)
    monkeypatch.setattr('tools.approval.restrict_gateway_approval_card_to_deny',
                        lambda key, request_id, restricted_=True: restricted.append((key, request_id)), raising=False)
    button = await device(adapter, buttons=True)
    short = await ask()
    assert restricted == []  # shown whole: Hermes' own rule decides
    await adapter._button_approval(button, {'id': 'r1', 'choice': 'once'})
    assert card == [(short.session_key, 'once', 'r1')] and calls == []
    clipped = await ask(id='r2', command=LONG)
    assert restricted == [(clipped.session_key, 'r2')]  # Hermes keeps it Deny-only for a card tap too
    await adapter._button_approval(button, {'id': 'r2', 'choice': 'once'})
    assert card == [(short.session_key, 'once', 'r1')]
    await adapter._button_approval(button, {'id': 'r2', 'choice': 'deny'})
    assert card[-1] == (clipped.session_key, 'deny', 'r2')
