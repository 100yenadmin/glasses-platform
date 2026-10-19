"""Grade the live-proof transcripts (JSON lines written by sim/glasses_sim.py). Prints PASS/FAIL per check."""
import json
import sys
from pathlib import Path

OUT = Path(sys.argv[1])
results = []


def frames(name):
    path = OUT / f'{name}.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def inbound(name, kind=None):
    return [r['frame'] for r in frames(name) if r['dir'] == 'in' and (kind is None or r['frame'].get('type') == kind)]


def query_id(name):
    return next((r['frame'].get('id') for r in frames(name) if r['dir'] == 'out' and r['frame'].get('type') == 'query'), None)


def check(name, ok, label):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {label}")


def one_reply(name, v1, must_contain=None):
    replies = inbound(name, 'response')
    ok = len(replies) == 1
    if ok and v1:
        ok = replies[0].get('final') is True and replies[0].get('turn') == query_id(name)
    if ok and must_contain:
        ok = must_contain in replies[0]['text']
    extra = f' containing "{must_contain}"' if must_contain else ''
    check(name, ok, f'exactly one reply{extra}' + (' (final, own turn)' if v1 else ''))


one_reply('A1-v1-text', True)
check('A2-v1-photo', [f.get('reason') for f in inbound('A2-v1-photo', 'capture_photo')] == ['keyword'], 'keyword capture_photo')
one_reply('A2-v1-photo', True, '42')
check('A2b-v1-inline', not inbound('A2b-v1-inline', 'capture_photo'), 'inline image needs no capture')
one_reply('A2b-v1-inline', True, '42')
check('A3-v1-tool', len(inbound('A3-v1-tool', 'session_reset')) == 1, 'new_session answered by session_reset')
check('A3-v1-tool', not any('Confirm /new' in f.get('text', '') for f in inbound('A3-v1-tool')), 'no /new confirm prompt reached the device')
check('A3-v1-tool', [f.get('reason') for f in inbound('A3-v1-tool', 'capture_photo')] == ['tool'], 'agent called glasses_capture_photo')
one_reply('A3-v1-tool', True, '42')
pushes = [inbound(n, 'push') for n in ('A4-v1-push', 'A4-v1-push-hud')]
check('A4-v1-push', all(len(p) == 1 and p[0]['origin'] == 'cron' for p in pushes) and pushes[0][0]['id'] == pushes[1][0]['id'],
      'cron push fanned out to both devices (same id)')
one_reply('A5-v0-text', False)
check('A6-v1-outbox', [p['origin'] for p in inbound('A6-v1-outbox', 'push')] == ['cron'], 'push queued while offline arrives on connect')
one_reply('B1-v0-text', False)
one_reply('B2-v1-text', True)
check('B2-v1-text', not inbound('B2-v1-text', 'response_delta'), 'no deltas with streaming off')
check('B3-v0-photo', inbound('B3-v0-photo', 'capture_photo') == [{'type': 'capture_photo'}], 'v0-shaped capture_photo')
one_reply('B3-v0-photo', False, '42')
check('B4-v0-push', any('Cron reminder' in f['text'] for f in inbound('B4-v0-push', 'response')), 'cron push reaches v0 as response')
for name in ('A5-v0-text', 'B1-v0-text', 'B3-v0-photo', 'B4-v0-push'):
    leaked = {f.get('type') for f in inbound(name)} & {'status', 'response_delta', 'push', 'hello_ack'}
    check(name, not leaked, 'v0 sees no v1-only frames' + (f' (got {sorted(leaked)})' if leaked else ''))
for chain in ('A7-chain', 'B5-chain'):
    first, second = query_id(f'{chain}-first'), query_id(f'{chain}-second')
    got = inbound(f'{chain}-second', 'response')
    check(f'{chain}-second', len(got) == 1 and got[0].get('turn') == second and '4' in got[0]['text'],
          'barge-in query gets exactly one reply, its own, containing "4"')
    firsts = inbound(f'{chain}-first', 'response')
    check(f'{chain}-first', len(firsts) <= 1 and all(f.get('turn') == first for f in firsts),
          f'interrupted query gets at most one reply, its own (got {len(firsts)})')
    check(chain, not [f for f in inbound(f'{chain}-second', 'push') + inbound(f'{chain}-first', 'push')],
          'no reply leaked out of band as a push')
print(f'checks: {sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
