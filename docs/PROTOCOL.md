# Glasses device protocol (v0 and v1)

Status: **draft for M0**. This is the contract between a glasses companion app and the `glasses` Hermes platform.

- **Transport:** one WebSocket per device at `ws(s)://<host>:<port><path>`. The default is `:8765/voice`.
- **Frames:** UTF-8 JSON text frames, each an object with a `type` field. In v0 the only binary frames are TTS
  audio (M1).

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
| `audio_start`, binary, `audio_end` | — | **M1.** Server TTS as PCM16 mono 24 kHz, only when the response says `tts:true` |

## v1 additions

### Handshake

```json
{"type":"hello","protocol":1,
 "app":{"name":"hermes-glasses-ios","version":"1.4.0"},
 "device":{"name":"rayban","model":"Ray-Ban Display",
           "capabilities":{"camera":true,"display":true,"speaker":true,"mic":true,"display_chars":40}}}
```

```json
{"type":"hello_ack","protocol":1,"user":"owner","device":"rayban","server":{"plugin":"glasses-platform","version":"0.1.0"}}
```

The server uses `capabilities.camera` to choose which device to ask for a photo. It never sends `capture_photo`
to a device that declared `camera:false`.

### Client → server

| Frame | Fields | Meaning |
|---|---|---|
| `query` | `id`, `text`, `image?` `{data, mime}`, `tts?` | Like v0, plus an optional inline photo and a client turn id |
| `photo` | `request`, `data`, `mime?` | Answer to `capture_photo` with the same `request` id |
| `photo_error` | `request`, `message?` | — |
| `interrupt` | — | **M1.** Stop the current turn (`/stop`) |
| `approval_response` | `id`, `choice` (`once`/`session`/`always`/`deny`) | **M1** |
| `ack` | `id` | **M1.** Push received; removes it from the durable outbox |

### Server → client

| Frame | Fields | Meaning |
|---|---|---|
| `status` | `state` (`thinking`/`working`/`idle`), `detail?` | Activity for the HUD. `detail` is the latest tool-progress or commentary line (≤200 chars); never speak it. A turn ends with `response`, or with `idle` if the agent produced no text. |
| `response_delta` | `turn`, `text` | **Cumulative** reply text so far (only when Hermes streaming is on). Render by replacing: it restarts when the agent begins a new segment after a tool call. |
| `response` | `turn`, `text`, `final:true`, `tts` | The authoritative reply: exactly one per `query`, sent when Hermes finishes the turn |
| `push` | `id`, `origin`, `text` | Out-of-turn message, fanned out to all of the user's live devices. `origin`: `message`, `cron`, `approval` (a text approval prompt), `clarify` (a question from the agent), `reply` (the reply to a query whose device went offline). Others may be added. |
| `capture_photo` | `request`, `reason` (`keyword`/`tool`) | Photo request |
| `approval_request` | `id`, `text`, `choices` | **M1** |

## Delivery rules

- Each `query` gets exactly one reply, sent when Hermes finishes the turn. Stream previews, tool progress,
  commentary and busy notices never end a turn: v1 sees them as `response_delta` or `status`, v0 not at all.
- A reply goes only to the device that sent the `query`, and so do that turn's `response_delta` and `status`
  frames. If that device went offline, the reply goes out as a `push` with origin `reply`.
- Pushes, approval prompts and clarify questions go to every live device of the same user (v0 gets them as
  `response`) and never to another user. Answer a prompt with an ordinary `query`, for example `/approve`.
- A query sent while another is running is either queued (answered after it, on its own device) or, in Hermes'
  default `interrupt` mode, folded into the running turn. A folded query gets the turn's reply, with its own turn
  id, on its own device; the interrupted query then gets no reply of its own (its device gets `status idle`).
  A command Hermes answers at once (`/stop`, `/status`) gets its own reply, and `new_session` always ends with
  `session_reset`. Replies are never split into several messages.
- A device that is connected but has not settled its protocol holds new pushes until it does. If no device is live,
  the server queues pushes (50 items, 24 h) and delivers them in order on the next connect. In M0 the queue is lost
  on server restart.
- Concurrent queries from one user are serialised by Hermes's session handling.

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
