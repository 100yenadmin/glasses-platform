# Python device client

Requires Python 3.11+ and aiohttp (already a plugin dependency). Install aiohttp
with `python -m pip install aiohttp` if needed. Put `hermes_glasses.py` alongside
your script and import it directly:

```python
import asyncio
import os
from hermes_glasses import HermesGlassesClient, HermesGlassesHttp

async def main():
    client = HermesGlassesClient(
        'ws://localhost:8765/voice', os.environ['DEVICE_TOKEN'],
        {'name': 'python', 'capabilities': {'display': True, 'ack': True}},
    )
    client.on('push', lambda frame: print(frame['text']))
    try:
        hello = await client.connect()  # hello['server']['features'] lists controls
        answer = await client.ask(text='Hello', speak=False)
        print(answer.text)
    finally:
        await client.disconnect()

asyncio.run(main())
```

This is a protocol library for scripts, Raspberry Pi devices, desktop tools and
Python bridges to microcontrollers. It does not run on MicroPython or provide a
hardware bridge. Checked against a real Hermes gateway (a text turn and a photo turn); real devices and
MicroPython have not been tested.

Set `client.on_capture_photo = lambda reason: base64_jpeg` for automatic camera
answers; async callables also work. Or listen for `capture_photo` and call
`send_photo(request, data)` / `photo_error(request, message)` yourself. These send
helpers and `interrupt()` queue frames synchronously on the running event loop.
Camera failures return a generic photo error. Listen for `approval_request`, show
its choices, and `await client.answer_approval(id, choice)` after the user chooses.
It returns the server's `approval_result`, including `ok:False`. `approval_closed`
dismisses a prompt answered elsewhere.

`on(name, fn)` receives a frame dict and returns an unsubscribe function; `off`
removes it. Listeners may be sync or async; failures do not stop the transport.
Events: `connected`, `disconnected`, `push`, `approval_request`, `approval_closed`,
`capture_photo`, `display`, `haptic`, `audio`, `status`, `delta`, `transcript`, `error`.
Delta text is cumulative: replace the displayed preview. `delta` and `transcript`
include `id` from `turn`. Status infers `id` only with one outstanding query.
Correlated errors affect only their query. Uncorrelated errors resolve queries
that have text and reject those without it. A reset fails on `New session failed.`
or an error with no queries in flight. Unknown fields and frame types are ignored.

`await ask(text='', audio=None, image=None, speak=None, id=None, timeout=180)` returns
`TurnResult(id, text, transcript, audio)`; absent transcript/audio are `None`.
Media uses dicts with `data` (base64) and `mime`; input audio may include `rate`.
Ids must be unique among pending asks and interrupts; defaults are unique per client.
Replies may arrive out of order. Messages up to 32 MiB are accepted (long spoken answers
arrive as one frame); pass `max_message_size=` to change it. Audio waits up to 15 seconds after text only when
both the device and server declare `audio_out`; `speak=False` skips that wait.
The total timeout is in seconds; `timeout=0` or `None` disables it. Timeouts raise
`HermesGlassesError` with `code='timeout'`. Cancelling an ask task removes only its
local wait and raises `HermesGlassesError` with `code='aborted'`.
`interrupt(ask=id)` sends a fresh stop turn and raises `Interrupted` on that ask
(`code='interrupted'`). Without an id it picks the oldest unanswered non-slash ask.
An interrupted ask waiting only for audio returns its text. Stop replies are consumed.

`await new_session()` coalesces simultaneous resets. Disconnect fails pending
operations (`code='disconnected'`); queries are never replayed. Server failures use
`code='server'`. Automatic reconnect repeats hello with jittered exponential backoff
from 1 to 30 seconds. Close 4401 and `await disconnect()` stop retries;
`reconnect=False` disables them. Invalid URLs fail once without retrying.

For platforms without a socket:

```python
http = HermesGlassesHttp('http://localhost:8765', os.environ['DEVICE_TOKEN'])
reply = await http.turn({'text': 'Hello'})
plain = await http.turn({'text': 'Hello'}, text_plain=True)
items = await http.pending(wait=25)
result = await http.approval(prompt_id, selected_choice)
```

HTTP returns JSON unchanged, including pending (202) and needs_answer responses.
Text mode returns `{'status': 'pending' or 'done', 'text': ...}`; only 202 maps to
pending. Use JSON to distinguish needs_answer. Non-success HTTP codes raise
`HermesGlassesError`. Both clients send Bearer authentication. Use HTTPS/WSS away
from localhost and keep tokens out of logs.

From the repository root, run `python -m pytest clients/python` with `HERMES_SOURCE`
pointing at a prepared Hermes checkout. Tests use localhost peers, including the
conformance kit. Hardware, TLS deployments and live reconnects are not proven.
