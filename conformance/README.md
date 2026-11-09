# Protocol conformance tools

Requires Python 3.11+ and aiohttp (already a plugin dependency).
From the repository root:

```sh
python -m conformance check-server --url ws://localhost:8765/voice --token "$DEVICE_TOKEN"
python -m conformance check-server --url ws://localhost:8765/voice --token "$DEVICE_TOKEN" --http http://localhost:8765 --json
python -m conformance serve --port 8766 --token "$FIXTURE_TOKEN"
```

The checker exits 1 on any FAIL and 0 on all PASS. Quick checks exercise authentication,
handshake metadata, malformed/unknown frames, ping, one real text turn ("Say hello."),
and `/new` via `new_session` (resets the live conversation).
Exactly-once is checked through a subsequent ping/pong barrier; it is a bounded
observation, not a proof against indefinitely delayed duplicates. It declares no
optional capabilities, so servers without planned voice/actions/approvals/ack support
can still pass. A missing server.features list fails the required handshake check.
`--http` adds authentication, malformed-body and empty pending-queue checks; start
with a quiet device queue. `--timeout` sets the normal request timeout in seconds
(default 10), applied to each WebSocket connection/receive step and HTTP request,
not the WebSocket lifetime. Checks not reached after a failure are reported SKIP.

`--full` needs a real agent and creates a real one-minute stretch reminder. It waits
up to 150 seconds for a push with `origin: 'cron'` after sending the request. Do not use full mode against
an account unless scheduling that reminder is intended. Unit tests simulate full mode
with fixture pushes; they do not schedule real reminders.

The fake server binds localhost, accepts Bearer authentication (case-insensitive
scheme, preferred over a URL `?token=`) or a URL token, and prints
PASS/FAIL after each scenario; it stays alive until interrupted. `--port 0` chooses
an available port and prints `READY <port>`. `--json` prints a JSON report per completed
connection (the READY line is still plain text). Reports contain check names only,
never credentials or media. A client has 20 seconds to finish the scenario.

The scenario checks hello, optional push ack, optional camera answer (JPEG markers
and base64, or photo_error), optional button choice, and a query. It sends cumulative
delta/status, optional transcript and valid silent WAV audio, display/haptic actions
when declared, and an unknown frame. It closes after required answers and a query.
The JPEG check is a framing check, not a full decoder. Use audio/wav in the fixture's
audio_out list. No actual command approval or device action is performed.
