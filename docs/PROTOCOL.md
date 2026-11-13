# Glasses device protocol (v0 and v1)

Status: **v1, extended in 0.4.0** (sections marked *planned* are specified but not yet released). This is the contract between a glasses companion app and the `glasses` Hermes platform.

- **Transport:** one WebSocket per device at `ws(s)://<host>:<port><path>`. The default is `:8765/voice`.
- **Frames:** UTF-8 JSON text frames, each an object with a `type` field. Binary data (photos, audio) travels
  base64-encoded inside JSON. Devices that cannot hold a WebSocket use the [HTTP route](#http-route).

## Authentication

- Send a per-device token as `Authorization: Bearer <token>` (preferred) or as `?token=<token>` (the v0 apps use this).
- The server maps the token to `user:device`. A missing or unknown token closes the socket with code **4401**
  (`unauthorized`).
- The server refuses to start when no tokens are configured.

## Versioning

1. On connect the server sends `{"type":"welcome","protocol_max":1}`. v0 apps ignore the extra field.
2. A client that sends no `hello` is treated as **v0** for the whole connection.
3. A client that wants **v1** sends `hello` as its first frame, within 2 seconds of `welcome`. The server answers
   `hello_ack`. Queued pushes wait until the protocol is known: until `hello`, until the first other frame, or for
   2 seconds, after which the connection counts as v0.
4. New fields may be added to any frame at any time. Clients and servers must ignore unknown fields and unknown
   frame types.

## v0 (compatible with the hermes-glasses iOS bridge)

### Client → server

| Frame | Fields | Meaning |
|---|---|---|
| `query` | `text`, `tts?` (bool) | One finished utterance. STT already happened on the phone. |
| `photo` | `data` (base64 JPEG) | Answer to `capture_photo` |
| `photo_error` | `message?` | The device could not take a photo |
| `new_session` | — | Forget the conversation; the server runs `/new` |
| `ping` | — | Keepalive |
| `debug` | `msg` | Free-form log line for the server log |

### Server → client

| Frame | Fields | Meaning |
|---|---|---|
| `welcome` | `protocol_max` | Connected and authenticated |
| `capture_photo` | — | Take a photo now and reply with `photo` or `photo_error` within the timeout (default 25 s) |
| `response` | `text`, `tts` (bool) | Reply text, exactly one per `query`. `tts:false` means the device speaks it itself. Unsolicited `response` frames are pushes (cron, reminders) and approval or clarify prompts. |
| `session_reset` | — | `/new` done |
| `pong` | — | Keepalive reply |
| `error` | `message` | Something went wrong, such as an empty query |

## v1 additions

### Handshake

```json
{"type":"hello","protocol":1,
 "app":{"name":"hermes-glasses-ios","version":"1.4.0"},
 "device":{"name":"rayban","model":"Ray-Ban Display",
           "capabilities":{"camera":true,"display":true,"speaker":true,"mic":true,"display_chars":40,
                           "buttons":true,"haptics":true,"audio_out":["audio/mpeg"],"ack":true}}}
```

```json
{"type":"hello_ack","protocol":1,"user":"owner","device":"rayban",
 "server":{"plugin":"glasses-platform","version":"0.6.0","features":["audio_in","audio_out","approval_buttons","ack","display","haptic"]}}
```

`server.version` is the plugin's real version and `server.features` lists what this server supports, so a client
can hide controls the server cannot use. See [Capabilities](#capabilities) for what each capability changes.

### Client → server

| Frame | Fields | Meaning |
|---|---|---|
| `query` | `id`, `text`, `image?` `{data, mime}`, `audio?` `{data, mime, rate?}`, `speak?`, `tts?` | Like v0, plus an optional inline photo, a client turn id, and spoken audio instead of `text` (see [Voice](#voice)) |
| `photo` | `request`, `data`, `mime?` | Answer to `capture_photo` with the same `request` id |
| `photo_error` | `request`, `message?` | — |
| `interrupt` | `id?` | Stop the current turn: handled as a `query` with the text `/stop` (and this string `id`, if given), so its `response` carries Hermes' confirmation |
| `approval_response` | `id`, `choice` | A button answer to `approval_request` (see [Approvals with buttons](#approvals-with-buttons)) |
| `ack` | `id` | Push received (only from devices that declared `ack:true`; see [Acknowledgements](#acknowledgements)) |

### Server → client

| Frame | Fields | Meaning |
|---|---|---|
| `status` | `state` (`thinking`/`working`/`idle`), `detail?` | Activity for the HUD. `detail` is the latest tool-progress or commentary line (≤200 chars); never speak it. A turn ends with `response`, or with `idle` if the agent produced no text. |
| `response_delta` | `turn`, `text` | **Cumulative** reply text so far (only when Hermes streaming is on). Render by replacing: it restarts when the agent begins a new segment after a tool call. |
| `response` | `turn`, `text`, `final:true`, `tts` | The authoritative reply: exactly one per `query`, sent when Hermes finishes the turn |
| `push` | `id`, `origin`, `text` | Out-of-turn message, fanned out to all of the user's live devices. `origin`: `message`, `cron`, `approval` (a command approval, asked as a yes/no question), `clarify` (a question from the agent), `reply` (the reply to a query whose device went offline). Others may be added. |
| `capture_photo` | `request`, `reason` (`keyword`/`tool`) | Photo request |
| `approval_request` | `id`, `text`, `command`, `description`, `choices` | A command approval, to devices that declared `buttons:true` |
| `approval_result` | `id`, `ok`, `choice?`, `message` | The answer to this device's `approval_response` |
| `approval_closed` | `id`, `choice?` | An approval was answered (on another device, by voice, or typed); dismiss it |
| `transcript` | `turn`, `text` | What the server heard in an audio `query` |
| `audio` | `ref`, `data`, `mime` | Spoken version of the `response` (`ref` = its `turn`) or `push` (`ref` = its `id`), to devices that declared `audio_out` |
| `display` | `text`, `seconds?` | The agent shows text on the device's display |
| `haptic` | `pattern` (`tap`/`double`/`long`) | The agent buzzes the device |

## Delivery rules

- Each `query` gets exactly one reply, sent when Hermes finishes the turn. Stream previews, tool progress,
  commentary and busy notices never end a turn: v1 sees them as `response_delta` or `status`, v0 not at all.
- A reply goes only to the device that sent the `query`, and so do that turn's `response_delta` and `status`
  frames. If that device went offline, the reply goes out as a `push` with origin `reply`.
- Pushes, approval prompts and clarify questions go to every live device of the same user (v0 gets them as
  `response`) and never to another user. Answer a prompt with an ordinary `query`. For a command approval a `query`
  whose whole text is a yes ("yes", "go ahead", "allow it", ...) allows the command once and a no ("no", "stop",
  "cancel", ...) blocks it; the device gets a short `response` ("Okay, going ahead.") to that query. Typed `/approve`
  and `/deny` also work. The prompt text carries the whole command when it is at most 300 characters. A longer
  command is clipped in the text, and then a device can only block it: a yes resolves nothing and is not passed to
  Hermes, and the user allows it with a typed `/approve` after reading the full command. The same holds for a
  spoken yes when a device that declared `audio_out` was sent the prompt as speech and Hermes's text-to-speech
  dropped part of the command (paths and web addresses). A client that cannot show or speak a prompt's whole text
  should not offer to approve it.
- v0 has no `push` frame, so a push sent while a v0 device waits for its answer would look like that answer. While
  a v0 device has a `query` or `new_session` in flight, pushes to it wait until its answer (`response`, `error` or
  `session_reset`) has gone out, then follow in order. They wait 120 s at most (`GLASSES_PUSH_HOLD_SECONDS`).
  Approval prompts and clarify questions are never held, because the turn is waiting for them. v1 devices get
  pushes at once.
- A query sent while another is running is either queued (answered after it, on its own device) or, in Hermes'
  default `interrupt` mode, folded into the running turn. A folded query gets the turn's reply, with its own turn
  id, on its own device; the interrupted query then gets no reply of its own (its device gets `status idle`).
  A command Hermes answers at once (`/stop`, `/status`) gets its own reply, and `new_session` always ends with
  `session_reset`, sent once Hermes has actually run `/new`. Right after a gateway restart Hermes may hold
  messages for a moment; wait for `session_reset` (or `error`, if Hermes could not start a new session) before
  sending the next `query`. Replies are never split into several messages.
- A cron push carries the job's own text. Hermes's header (job name and id) and footer (how to stop the job) are
  removed, because devices read pushes aloud.
- A device that is connected but has not settled its protocol holds new pushes until it does. If no device is live,
  the server queues pushes (50 items, 24 h) and delivers them in order on the next connect. Since 0.3.0 the queue
  survives a server restart (`GLASSES_OUTBOX_PERSIST=off` turns that off).
- Concurrent queries from one user are serialised by Hermes's session handling.

## Capabilities

A v1 device declares what it has in `hello.device.capabilities`. Every field is optional; a missing field means
"no". The server never sends a device something it did not declare.

| Capability | Type | What the server does with it |
|---|---|---|
| `camera` | bool | Asks this device for photos (`capture_photo`). v0 devices are assumed to have one. |
| `display`, `display_chars` | bool, int | Allows `display` frames; `display_chars` is the width of one line, for the agent's hint. |
| `speaker`, `mic` | bool | Informational: the device speaks text and listens itself. |
| `audio_out` | list of MIME types | The device wants the server to speak for it: every `response` and `push` to it is followed by an `audio` frame in the first listed format the server can produce (`audio/mpeg`, `audio/ogg` with Opus, `audio/wav`). |
| `buttons` | bool | Command approvals arrive as `approval_request` (with choices) instead of a spoken yes/no `push`. |
| `haptics` | bool | Allows `haptic` frames. |
| `ack` | bool | The device acknowledges pushes; see [Acknowledgements](#acknowledgements). |

## Voice

**Voice in.** A device that does no speech recognition sends the recording in `query.audio`:
`{"data": base64, "mime": "audio/wav"}`. Accepted: `audio/wav`, `audio/ogg` (Opus or Vorbis), `audio/mpeg`,
`audio/mp4`, `audio/webm`, `audio/flac`, and `audio/pcm` (raw 16-bit little-endian mono; `rate` gives the sample
rate, default 16000). `text` may be empty. The server transcribes with Hermes's own speech-to-text (the `stt`
settings in Hermes's config), sends `transcript` to the asking device, then handles the words exactly like a typed
`query`: a spoken "yes" still answers an approval, "what am I looking at" still takes a photo. When nothing could
be heard the device gets `error` ("I couldn't hear that.") as the answer to that query; in v1 it carries the query's
id as `turn`, so a client with other queries in flight knows which one failed.

**Voice out.** The server speaks with Hermes's own text-to-speech (the `tts` settings). Devices that declared
`audio_out` get an `audio` frame after each `response` and `push`; a `query` with `speak:false` turns it off for
that one reply. Audio is a whole reply, never a stream, and is sent after the text, so a device can show the text
first. If speech fails the text has still arrived; no `audio` frame follows.

```
C→S {"type":"query","id":"q7","text":"","audio":{"data":"UklGR...","mime":"audio/wav"}}
S→C {"type":"transcript","turn":"q7","text":"what's on my calendar today"}
S→C {"type":"status","state":"thinking"}
S→C {"type":"response","turn":"q7","text":"Two meetings: design at ten, lunch with Sam at one.","final":true,"tts":false}
S→C {"type":"audio","ref":"q7","mime":"audio/mpeg","data":"SUQzBAAAAA..."}
```

## Approvals with buttons

A command approval goes to each of the user's live devices. A v1 device that declared `buttons:true` gets
`approval_request`; every other device gets the spoken yes/no `push` (origin `approval`) as before. Offline devices
get the `push` from the queue.

```
S→C {"type":"approval_request","id":"a1b2","text":"Hermes wants to run: rm -rf build. Recursive delete.",
     "command":"rm -rf build","description":"recursive delete","choices":["once","session","always","deny"]}
C→S {"type":"approval_response","id":"a1b2","choice":"once"}
S→C {"type":"approval_result","id":"a1b2","ok":true,"choice":"once","message":"Okay, going ahead."}
```

- `id` is Hermes's own request id, so a button can only ever answer that request.
- `choices` are the ones Hermes offers for this command (a smart-deny offers only `once` and `deny`). A choice that
  is not in the list, an unknown or expired `id`, or another user's `id` gets `ok:false` and changes nothing.
- When `text` could not carry the whole command (over 300 characters), `choices` is only `["deny"]`, even though
  `command` holds the whole command: approving it takes a typed `/approve`.
- When an approval is answered anywhere (a button, a spoken yes or no, a typed `/approve`), the user's other v1
  devices that got the `approval_request` receive `approval_closed`.

## Device actions

The agent can show text (`display`) or buzz (`haptic`) on the user's devices with the `glasses_show_text` and
`glasses_buzz` tools. During a turn the action goes to the asking device if it declared the capability, otherwise
to the user's other live devices that did. Actions never reach another user's devices and are never queued.

## Acknowledgements

A device that declared `ack:true` answers each `push` with `{"type":"ack","id":<push id>}`. Until the ack arrives
the push stays in the server's queue: if the device disconnects first, the push is sent again on the next
connect. Devices that do not declare `ack` keep the default: a push counts as delivered once it is written to
the socket.

## HTTP route

For devices that cannot keep a WebSocket open (Apple Watch, Garmin, Siri Shortcuts, scripts). Same listener, same
device tokens (`Authorization: Bearer <token>`, or `?token=`). Bodies are JSON.

- `POST /turn` with `{"text"}` or `{"audio":{...}}`, plus optional `id`, `image`, `speak`, and `capabilities`
  (as in `hello`, for this request). Answers when the turn ends, with
  `{"id","status":"done","text","transcript?","audio?"}`. If the agent needs an answer first (an approval or a
  clarify question), it answers early with `"status":"needs_answer"` and the prompt in `"prompts"`; answer it with
  another `POST /turn` (for example `{"text":"yes"}`) or with `POST /approval`. If the turn is still running after
  `turn_timeout` seconds (default 50), it answers `202` with `"status":"pending"`, and the reply arrives later
  through `/pending`. With `Accept: text/plain` the body is just the reply text (or the prompts), for Shortcuts;
  pushes it does not show stay queued for `/pending`. A request has no camera, display or haptics: those
  capabilities are ignored here. With `audio_out`, a JSON answer carries audio for the turn's reply only, never for
  pushes.
- `GET /pending?wait=25` waits up to `wait` seconds (at most 55) for pushes and answers
  `{"items":[<push frames>]}` as soon as there is at least one, or an empty list at the deadline. Pushes are
  removed from the queue once they are in an answer.
- `POST /approval` with `{"id","choice"}` answers like `approval_response` and returns the `approval_result`.
- Each request is its own short-lived connection: it never replaces the device's WebSocket or another open request.
  While it is open the device counts as connected, so pushes for it go into that request's answer instead of the
  queue. They count as delivered only once the answer has been written, so a gateway restart during the wait
  keeps them. If the answer cannot be written (the client went away), its pushes and the turn's reply go back to the
  queue. A request that has already answered never loses a push: anything that arrives later waits in the
  queue for the next request.
- A push can arrive twice (in an open `/turn` and an open `/pending` of the same device, or again after an answer
  that could not be written): drop repeats by `id`.
- Status codes: `401` bad or missing token, `400` malformed body, `413` too large, `202` still running.

## Omi webhook

Set Omi's **Real-Time Transcript Webhook** to `https://<public-host>/omi/<device-token>/transcript`.
Omi's backend must reach it over the internet (an HTTPS reverse proxy or Tailscale Funnel); local network
access alone is insufficient. See [setup](../README.md#omi-pendant). The plugin makes no outbound Omi calls.

- Omi calls `POST /omi/{token}/transcript?session_id=<id>&uid=<omi-user-id>` repeatedly during a live
  conversation. The path token uses the same authentication as the WebSocket and decides the Hermes user
  and device. `uid` is ignored; `session_id` only groups transcript segments for that authenticated device.
- The body is a JSON **array**, for example
  `[{"text":"Hey Hermes, what time is it","speaker":"SPEAKER_00","speakerId":0,"is_user":true,"start":0,"end":2}]`.
  Malformed JSON or segments get `400`, a bad token gets `401`, and the listener's body limit still applies (`413`).
- A segment starting with “hey hermes” or “hermes” starts a question, ignoring case, leading punctuation and a
  leading “ok” or “okay”. The wake word must be followed by a comma, the end of the segment, or a space
  whose following text does not start with `'s` or `’s`; “Hermes's docs are great” does not wake it.
  The following words become the query. A wake phrase alone arms an eight-second
  window for subsequent segments in that session; eligible segments in the completing call are joined.
  A call with any `is_user:true` segments uses only those segments; otherwise all speakers count for questions.
  Only a question made entirely of `is_user:true` speech (the wearer's own voice) can answer an approval
  (since 0.6.2). Repeated segments with the same start time and text are ignored within that device's
  session, retaining the newest 256 keys. After ten minutes without a call, session state
  expires (pruned on the next call). Each device identity keeps at most 32 sessions, evicting the least
  recently used.
- Each question is an ordinary v1 query on a short-lived connection with capabilities `{}`. The webhook
  waits up to `omi_wait` seconds (`GLASSES_OMI_WAIT`, default 20) for its reply or an approval/clarify prompt.
  It always answers a valid call with `200`: `{"message":"<text>"}` when there is text, otherwise `{}`.
  Every non-empty message starts with `Hermes: ` so it exceeds Omi's five-character display threshold.
  A late reply becomes a queued
  push for a later call. Approval prompts use the existing spoken path: “Hey Hermes, yes” or “Hey Hermes, no”.
- Every call, including an empty array or one without a wake phrase, flushes this user's queued pushes.
  Pushes and replies are joined in arrival order (oldest first), separated by blank lines, with a total limit
  of 1000 characters including the prefix. An item exceeding the remaining room keeps its head and ends
  with `…`; it counts as shown and is acknowledged after a successful write. Items with no room stay queued
  for the next call. Overlapping webhook calls can show the same push twice.
  If a write fails, pushes and replies are queued again. The same
  HTTP finish-under-lock and release helpers keep pushes arriving at the deadline for the next call.
- Delivery requires transcript calls: while the pendant is offline or no live conversation is running,
  messages wait in the normal outbox. This route does not replace a WebSocket or another open request.

## Bobby (Pebble) endpoint

The [Bobby assistant](https://github.com/pebble-dev/bobby-assistant) can use this listener without a protocol patch:
set its `QUERY_URL` to `wss://<host>/bobby/<token>/query` and `QUOTA_URL` to
`https://<host>/bobby/<token>/quota`. Authenticate with the plugin device token in the path, never the `token`
query parameter (Bobby's Pebble timeline token, ignored). No headers are required; `Origin: null` is accepted.

- `GET /bobby/{token}/quota` returns `200 {"used":0,"remaining":1000000,"hasSubscription":true}`; bad token: `401`.
- `GET /bobby/{token}/query?prompt=...` upgrades to WebSocket. Bad tokens are accepted, sent `d`, then closed with
  code `1008` and reason `Unauthorized`, which Bobby forwards to the watch. Missing/blank prompt: `wNo question.`,
  `d`, then close `1000`.
- A valid prompt becomes a normal v1 `query`, with empty capabilities and a separate short-lived registry slot.
  `threadId` and all other Bobby metadata are ignored; Hermes keeps the user's shared conversation.
- Text frames: `fThinking…` once, then `c<new text>` chunks preserving whitespace. Hermes's cumulative deltas
  become increments, and the final response sends only its remaining text. Complete `<<!!WIDGET:{json}!!>>`
  markers are stripped, including markers split across deltas. No `a` action frames are sent; client frames are ignored.
- Queued pushes follow the answer as `c\n\nReminder: <text>` (`cron`) or `c\n\n<Origin>: <text>`. Finish with one
  `d`, then `t<uuid>` and close `1000`. Approval/clarify prompts are plain chat text followed by the same finish;
  the next question can answer yes/no. An agent error is a `w` warning followed by the same finish.
- `bobby_wait` / `GLASSES_BOBBY_WAIT` defaults to 45 seconds. On timeout, send
  `cStill working; ask again in a moment for the answer.`, then the same pushes/finish. The late reply is queued
  by the adapter and shown with the next answer.
- Pushes count as shown only after their chat frames and `d` are successfully sent. A failure before `d` requeues
  every push plus any completed turn reply; a still-running reply follows through the adapter's offline queue.
  This proves a server write, not watch receipt. Widgets and watch-side actions are unsupported.

See [Pebble (Bobby)](../README.md#pebble-bobby) for build steps and phone reachability requirements.

## Limits

- **Frame size:** the server's maximum is 8 MB by default (`GLASSES_MAX_FRAME_MB`). Downscale photos to about
  1600 px on the long side.
- **Malformed frames:** non-object JSON and unknown types are ignored and logged. They never close the socket.
- **Liveness:** the server sends WebSocket pings every 30 s. Clients may also use `ping`/`pong` frames.

## Example (v0 photo turn)

```
S→C {"type":"welcome","protocol_max":1}
C→S {"type":"query","text":"what am I looking at?"}
S→C {"type":"capture_photo"}
C→S {"type":"photo","data":"/9j/4AAQ..."}
S→C {"type":"response","text":"A red bicycle leaning on a brick wall.","tts":false}
```

## xiaozhi devices

Stock xiaozhi-esp32 firmware discovers this transport through `POST /xiaozhi/ota/{token}` with its system-info
JSON. The same paired device credential authenticates OTA and `GET /xiaozhi` (Bearer header or `?token=`).
OTA returns only `websocket` (`url`, `token`, `version:1`), `server_time` (UTC milliseconds, offset 0), and
`firmware` (the reported `application.version`, or `"0"`, with an empty download URL). No activation or MQTT is
needed. The WebSocket URL uses the request Host and HTTPS / `X-Forwarded-Proto: https` for `wss`; override it
with `platforms.glasses.extra.xiaozhi_url` or `GLASSES_XIAOZHI_URL` behind a proxy. Treat the OTA URL as a secret.

Within 10 seconds the device sends `hello` and its Opus `audio_params`; the server replies with a fresh
`session_id`, `transport:websocket`, and Opus 24 kHz mono / 60 ms output. `Protocol-Version` overrides the hello's
`version` (default 1). Binary v1 is raw Opus; v2 uses the 16-byte version/type/reserved/timestamp/size header,
v3 the 4-byte type/reserved/size header, with multibyte fields in network order. Non-audio or malformed binary
headers are ignored. The input parameters are retained for wrapping microphone packets in Ogg Opus.

`listen start` collects packets; `listen stop` submits one v1 audio/ogg query, with an empty text and a fresh id.
Only recordings containing a speech-sized packet (over 12 bytes and over half a byte per millisecond of frame,
for example over 30 bytes at 60 ms or 12 bytes at 20 ms) are submitted. Size cannot tell constant-bitrate
silence from speech, so a silent recording may still be transcribed (and answered with "I couldn't hear that."). In `auto` / `realtime`, 800 ms of quiet
packets (any packet that is not speech-sized, after speech) also ends the recording; libopus codes silence at
16 kb/s in 0.3-0.4 byte per ms, so variable-bitrate silence and sent DTX packets count as quiet (firmware that sends nothing during DTX
reaches the time limit instead). Constant-bitrate silence
and steady background noise do not, and then the recording runs to its limit. Recordings end at 15 seconds of wall or
audio time, or at 240,000 bytes of Opus. Packets over 1500 bytes are ignored, and a WebSocket message over 64 KiB
closes the connection. `listen detect`, MCP and unknown types are ignored. `abort`
cancels queued/current playback; it submits `/stop` only if this connection owns the active unanswered turn.

The registered v1 connection declares `audio_out:["audio/mpeg"]`, speaker, mic and display. `transcript` becomes
`stt`; thinking/working status becomes one thinking `llm` expression per turn. Responses, pushes and errors are
spoken in order as `tts start`, `sentence_start` with text, binary Opus, `tts stop`. A matching MPEG `audio.ref`
is awaited for up to 20 seconds from enqueue; without it (or for errors), only the text events are sent.
`ffmpeg` converts MPEG to 24 kHz mono / 60 ms Opus, paced at about 55 ms per packet; missing ffmpeg falls back to
text events with one warning. `display` becomes a neutral Hermes `alert`; other adapter frames are dropped.
A push counts as delivered once its `tts stop` has been sent; a push that was aborted or not finished when the
device disconnects goes back to the queue for the next connect (or another device). The device itself sends no
playback acknowledgement, so a disconnect can repeat a partially heard push.

`sim/xiaozhi_sim.py --ota <private-OTA-URL> --wav sim/sample-voice.wav --out reply.ogg` exercises the device flow.
It prints incoming JSON, uploads Opus in manual mode and saves reply packets as Ogg. `--text-only` listens and
prints without uploading a recording or saving audio. The bundled WAV is a generated two-second 16 kHz mono tone.
Compatibility tests use fake adapter voice frames; physical firmware playback and live Hermes voice are unproven.
