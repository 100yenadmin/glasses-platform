# JavaScript device client

No runtime dependencies. Import the ES module directly in a browser or Node 22+:

```js
import {HermesGlassesClient, HermesGlassesHttp} from './hermes-glasses.js';
const client = new HermesGlassesClient({
  url: 'ws://localhost:8765/voice', token: deviceToken,
  device: {name: 'web', capabilities: {display: true, ack: true}},
});
client.on('push', frame => console.log(frame.text));
const hello = await client.connect(); // hello.server.features describes available controls
const answer = await client.ask({text: 'Hello', speak: false});
console.log(answer.text);
client.disconnect();
```

Even Hub web apps can import this same module. Pebble phone-side runtimes that do not
provide standard WebSocket/fetch can inject `WebSocketImpl` or `fetchImpl` through a
platform shim; the shim must implement the interfaces in `hermes-glasses.d.ts`.
This is a protocol library, not a hardware bridge. Browser builds need no bundler.

Register `client.onCapturePhoto = async reason => base64Jpeg` for automatic camera
answers, or listen for `capture_photo` and call `sendPhoto(request, data)` /
`photoError(request, message)` yourself. Camera failures return a generic photo error.
Listen for `approval_request`, show its choices, and call `answerApproval(id, choice)`
only after the user chooses. It resolves to the server's `approval_result`, including
`ok:false`. `approval_closed` dismisses a prompt answered elsewhere.

`on(name, fn)` receives a plain frame and returns an unsubscribe function; `off`
removes it. Events: `connected`, `disconnected`, `push`, `approval_request`,
`approval_closed`, `capture_photo`, `display`, `haptic`, `audio`, `status`, `delta`,
`transcript`, `error`. Delta text is cumulative: replace the displayed preview.
`delta` and `transcript` include `id` from `turn`. The protocol's `status` has no
turn id; `id` is inferred only while exactly one query is outstanding. Uncorrelated
server errors reject all outstanding queries and a pending reset; correlated error
extensions reject only their matching query. No new correlation fields are sent.

Each `ask` uses a unique id. Responses resolve with `{id, text, transcript?, audio?}`.
Devices declaring `audio_out` wait at most 15 seconds after text for audio; missing
speech still resolves the text. `speak:false` skips that wait. `audio` events also
include push audio. Unknown types/fields are ignored. Disconnect rejects pending
operations; queries are never replayed. Reconnect uses jittered exponential backoff
from 1 to 30 seconds and repeats hello; 4401 stops retries. `disconnect()` stops
retries. `newSession()` coalesces simultaneous resets because session_reset has no id.
A query folded by Hermes into another turn may receive only `status idle`, as
specified by the protocol; that does not resolve its ask promise. Applications
should avoid overlapping asks and use disconnect to cancel outstanding work.

For platforms without a socket:

```js
const http = new HermesGlassesHttp({baseUrl: 'http://localhost:8765', token: deviceToken});
const reply = await http.turn({text: 'Hello'});
const text = await http.turn({text: 'Hello'}, {textPlain: true});
const {items} = await http.pending(25);
await http.approval(promptId, selectedChoice);
```

HTTP helpers send Bearer authentication, preserve pending/needs_answer bodies (202
is successful), and throw on non-success status codes. WebSockets use a URL token;
keep connect URLs out of logs and use HTTPS/WSS away from localhost. Socket and fetch
implementations must provide the standard browser API surface (or a matching shim).

Run `PYTHON=python3 node --test clients/js/test/` from the repository root. The tests
start the Python conformance peer on localhost. No hardware or real agent is tested.
