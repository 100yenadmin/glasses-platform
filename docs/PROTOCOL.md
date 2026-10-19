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
3. A client that wants **v1** sends `hello` as its first frame. The server answers `hello_ack`.
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
| `response` | `text`, `tts` (bool) | Reply text. `tts:false` means the device speaks it itself. Unsolicited `response` frames are pushes (cron, reminders). |
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
| `status` | `state` (`thinking`/`working`/`idle`), `detail?` | Typing indicator for the HUD |
| `response_delta` | `turn`, `text` | **Cumulative** streamed text so far. Prefix-stable; render by replacing. |
| `response` | `turn`, `text`, `final:true`, `tts` | The authoritative final reply |
| `push` | `id`, `origin` (`cron`/`heartbeat`/`async`/`message`), `text` | Out-of-turn message. Fanned out to all of the user's live devices. |
| `capture_photo` | `request`, `reason` (`keyword`/`tool`) | Photo request |
| `approval_request` | `id`, `text`, `choices` | **M1** |

## Delivery rules

- A reply goes only to the device that sent the `query`.
- Pushes go to every live device of the same user and never to another user.
- If no device is live, the server queues pushes (50 items, 24 h) and delivers them in order on the next connect.
  In M0 the queue is lost on server restart.
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
