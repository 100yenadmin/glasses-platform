# M0 live proof

**Claim class:** advisory, loopback only. This proves the Hermes side of the channel against the bundled simulator and
a real Hermes gateway with real models.

**Not proven:** real glasses, the hermes-glasses iOS app, Meta's Wearables Device Access Toolkit, networking beyond
loopback (Tailscale, TLS), server-side TTS, native approval frames, a durable outbox, and model providers other than
the one below.

## Setup

- Hermes Agent `v0.22.2`, run as a real gateway in a throwaway home with its own lock dir (nothing shared with any
  other Hermes on the host).
- Plugin commit `27a1394`; run header: `pin v0.22.2, plugin 27a1394, port 18765`.
- Models: z.ai GLM coding plan, `glm-5.3` as the main model (text only) and `glm-4.6v` as `auxiliary.vision`.
- Devices: `sim/glasses_sim.py`, as v1 and v0 (legacy) clients, two device tokens for one user. The agent gets only
  the `vision` and `glasses` toolsets.
- Phase A runs with Hermes streaming on, phase B with it off. Photo turns use a generated sign reading "GATE 42"
  ([sign.jpg](m0-live-proof/sign.jpg)).

## Result: 28/28 checks passed

| Scenario | Check | Result | Receipt |
|---|---|---|---|
| A1-v1-text | exactly one reply (final, own turn) | ✅ | [A1-v1-text.jsonl](m0-live-proof/A1-v1-text.jsonl) |
| A2-v1-photo | keyword capture_photo | ✅ | [A2-v1-photo.jsonl](m0-live-proof/A2-v1-photo.jsonl) |
| A2-v1-photo | exactly one reply containing "42" (final, own turn) | ✅ | [A2-v1-photo.jsonl](m0-live-proof/A2-v1-photo.jsonl) |
| A2b-v1-inline | inline image needs no capture | ✅ | [A2b-v1-inline.jsonl](m0-live-proof/A2b-v1-inline.jsonl) |
| A2b-v1-inline | exactly one reply containing "42" (final, own turn) | ✅ | [A2b-v1-inline.jsonl](m0-live-proof/A2b-v1-inline.jsonl) |
| A3-v1-tool | new_session answered by session_reset | ✅ | [A3-v1-tool.jsonl](m0-live-proof/A3-v1-tool.jsonl) |
| A3-v1-tool | no /new confirm prompt reached the device | ✅ | [A3-v1-tool.jsonl](m0-live-proof/A3-v1-tool.jsonl) |
| A3-v1-tool | agent called glasses_capture_photo | ✅ | [A3-v1-tool.jsonl](m0-live-proof/A3-v1-tool.jsonl) |
| A3-v1-tool | exactly one reply containing "42" (final, own turn) | ✅ | [A3-v1-tool.jsonl](m0-live-proof/A3-v1-tool.jsonl) |
| A4-v1-push | cron push fanned out to both devices (same id) | ✅ | [A4-v1-push.jsonl](m0-live-proof/A4-v1-push.jsonl) |
| A5-v0-text | exactly one reply | ✅ | [A5-v0-text.jsonl](m0-live-proof/A5-v0-text.jsonl) |
| A6-v1-outbox | push queued while offline arrives on connect | ✅ | [A6-v1-outbox.jsonl](m0-live-proof/A6-v1-outbox.jsonl) |
| B1-v0-text | exactly one reply | ✅ | [B1-v0-text.jsonl](m0-live-proof/B1-v0-text.jsonl) |
| B2-v1-text | exactly one reply (final, own turn) | ✅ | [B2-v1-text.jsonl](m0-live-proof/B2-v1-text.jsonl) |
| B2-v1-text | no deltas with streaming off | ✅ | [B2-v1-text.jsonl](m0-live-proof/B2-v1-text.jsonl) |
| B3-v0-photo | v0-shaped capture_photo | ✅ | [B3-v0-photo.jsonl](m0-live-proof/B3-v0-photo.jsonl) |
| B3-v0-photo | exactly one reply containing "42" | ✅ | [B3-v0-photo.jsonl](m0-live-proof/B3-v0-photo.jsonl) |
| B4-v0-push | cron push reaches v0 as response | ✅ | [B4-v0-push.jsonl](m0-live-proof/B4-v0-push.jsonl) |
| A5-v0-text | v0 sees no v1-only frames | ✅ | [A5-v0-text.jsonl](m0-live-proof/A5-v0-text.jsonl) |
| B1-v0-text | v0 sees no v1-only frames | ✅ | [B1-v0-text.jsonl](m0-live-proof/B1-v0-text.jsonl) |
| B3-v0-photo | v0 sees no v1-only frames | ✅ | [B3-v0-photo.jsonl](m0-live-proof/B3-v0-photo.jsonl) |
| B4-v0-push | v0 sees no v1-only frames | ✅ | [B4-v0-push.jsonl](m0-live-proof/B4-v0-push.jsonl) |
| A7-chain-second | barge-in query gets exactly one reply, its own, containing "4" | ✅ | [A7-chain-second.jsonl](m0-live-proof/A7-chain-second.jsonl) |
| A7-chain-first | interrupted query gets at most one reply, its own (got 0) | ✅ | [A7-chain-first.jsonl](m0-live-proof/A7-chain-first.jsonl) |
| A7-chain | no reply leaked out of band as a push | ✅ | [A7-chain-first](m0-live-proof/A7-chain-first.jsonl), [second](m0-live-proof/A7-chain-second.jsonl) |
| B5-chain-second | barge-in query gets exactly one reply, its own, containing "4" | ✅ | [B5-chain-second.jsonl](m0-live-proof/B5-chain-second.jsonl) |
| B5-chain-first | interrupted query gets at most one reply, its own (got 0) | ✅ | [B5-chain-first.jsonl](m0-live-proof/B5-chain-first.jsonl) |
| B5-chain | no reply leaked out of band as a push | ✅ | [B5-chain-first](m0-live-proof/B5-chain-first.jsonl), [second](m0-live-proof/B5-chain-second.jsonl) |

Per-scenario exit codes and the token-leak check are in [SUMMARY.txt](m0-live-proof/SUMMARY.txt). Transcripts are the
simulator's JSON lines; photo bytes are elided.

## Also green at this commit

- Unit tests: 76 passed, 1 skipped.
- Integration test (real gateway, scripted fake model): text, photo, cron push and SIGTERM drain pass.
- `hermes plugins validate`: passed.
- Guard mutations: 25 of 25 went red. Each delivery guard was re-broken one at a time and its test went red.

## Re-run

```bash
HERMES_SOURCE=<hermes-agent v0.22.2 checkout with .venv> GLM_API_KEY=<key> \
  tests/live/live_proof.sh <empty-scratch-dir> <empty-out-dir>
python tests/live/live_checks.py <out-dir>
```
