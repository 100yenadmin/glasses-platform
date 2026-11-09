# Changelog

Each release has full notes on the [releases page](https://github.com/100yenadmin/glasses-platform/releases).

## Unreleased

- **Omi pendant.** Say “Hey Hermes, …” in a live Omi conversation to ask your agent through
  `POST /omi/{token}/transcript`. Answers, reminders and approval prompts return as Omi notifications; slow
  replies wait for the next transcript call. See [Omi setup](README.md#omi-pendant).
- **Omi webhook fixes.** Prefix notifications with `Hermes: `, keep item heads oldest first within 1000
  characters, acknowledge truncated pushes, and queue items with no room. Bound idle sessions and segment
  deduplication, reject possessive wake words, and return `400` for deeply nested malformed JSON.
- **ESP32 voice boxes (xiaozhi).** Boards running the open-source xiaozhi firmware pair with
  `hermes glasses pair me voicebox --xiaozhi` and the device's OTA address: push to talk with Opus audio, spoken
  answers, abort, and reminders played the next time the device connects. See
  [ESP32 voice boxes](README.md#esp32-voice-boxes-xiaozhi).

## 0.4.0 (2026-11-09): voice, approval buttons and an HTTP route

- **Voice in and out.** A v1 device can send a recording (`query.audio`); Hermes's own speech-to-text transcribes it
  and the words are handled exactly like typed text (spoken approvals and photo questions still work). Devices that
  declare `audio_out` get each answer and push as audio from Hermes's own text-to-speech.
- **Approval buttons.** Devices that declare `buttons` get `approval_request` cards with Hermes's choices, answered
  with `approval_response` and bound to Hermes's request id; other devices of the user are told when it closes.
- **Acknowledgements.** Devices that declare `ack` keep each push in the queue until they confirm it.
- **Device actions.** New agent tools `glasses_show_text` and `glasses_buzz`, only for devices that declared a
  display or haptics.
- **HTTP route** for devices that cannot keep a WebSocket open (Apple Watch, Siri Shortcuts, scripts): `POST /turn`,
  long-poll `GET /pending` and `POST /approval`, with the same device tokens.
- `hello_ack` reports the real plugin version and the server's features.
- Roadmap: the plugin becomes the hub for every non-desktop Hermes device. See [ROADMAP.md](ROADMAP.md) and the device
  survey in [docs/DEVICES.md](docs/DEVICES.md).
- Docs: the protocol spec for voice in and out, approval buttons, acknowledgements, device actions, capabilities and
  the HTTP route ([docs/PROTOCOL.md](docs/PROTOCOL.md)).
- Docs: "Ask Hermes" from an Apple Watch with a Siri Shortcut ([docs/devices/apple-watch.md](docs/devices/apple-watch.md)).

## 0.3.2 (2026-11-09): Hermes Glasses

The plugin's display name is now "Hermes Glasses", with a new banner. The plugin id (`glasses-platform`), the
`glasses` channel and every setting are unchanged.

## 0.3.1 (2026-11-09): clean install

Hermes's install scanner rated the README "caution" because of one phrase, which blocked installs without a terminal.
The wording is fixed, and a test now runs the scanner on every change.

## 0.3.0 (2026-11-09): reminders survive a restart

Pushes waiting for an offline device are kept in a file in the Hermes home and delivered after a gateway restart.
A crash can repeat a push but never lose one. `GLASSES_OUTBOX_PERSIST=off` keeps the queue in memory only.

## 0.2.1 (2026-11-09): a stop button for the glasses

A v1 `interrupt` frame stops the current turn (Hermes's own `/stop`).

## 0.2.0 (2026-11-09): pair in one command, try it from the terminal, approve by voice

- `hermes glasses pair | devices | revoke | try`: mint a device token with a QR code, list and revoke devices, and
  talk to your agent from the terminal as a pair of glasses with a camera.
- A dangerous-command approval is asked as a yes/no question, and a spoken "yes" (allow once) or "no" answers it.

## 0.1.3 (2026-11-08)

Pushes no longer interrupt a v0 device that is waiting for its answer: they follow once the answer has gone out.
The listener binds with public aiohttp calls only.

## 0.1.2 (2026-11-08)

Installs on core Hermes: `aiohttp` is declared as a dependency.

## 0.1.1 (2026-11-07)

`session_reset` waits until Hermes has really run `/new`; cron pushes carry only the job's own words; fewer stray
photos; the new-session button only confirms Hermes's `/new`.

## 0.1.0 (2026-11-07)

First release: glasses as a Hermes gateway channel over one WebSocket, compatible with the hermes-glasses iOS app
(v0) plus a richer v1 (streamed replies, pushes, status), photos on request and by tool, and an offline queue.
