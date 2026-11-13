# Security

Please report vulnerabilities privately: [open a security advisory](https://github.com/100yenadmin/glasses-platform/security/advisories/new).
Do not put security problems, device tokens, connect URLs, QR codes or photos in public issues.

We aim to acknowledge reports within 3 business days and to share a decision or fix plan within 14 days.

## Security report scope

- A request accepted without a valid device token, on any route: the `/voice`, `/xiaozhi` and `/bobby/<token>/query`
  WebSockets, or the HTTP `/turn`, `/pending`, `/approval`, `/omi/<token>/transcript`, `/xiaozhi/ota/<token>` and
  `/bobby/<token>/quota` routes.
- The listener starting with no device token configured.
- One user's device reaching another user's session: their replies, pushes, approvals, clarify questions or photos.
- An approval answered by anything other than that user's own device, or approved when the command was shown or
  spoken clipped (a clipped command can only be denied from a device).
- A device token written to a log, a reply, an error frame or any file other than the profile's `.env`.
- A glasses photo taken outside a glasses conversation, or sent anywhere other than Hermes's own image cache and the
  conversation it belongs to.
- The offline queue (`$HERMES_HOME/glasses/outbox.json`) readable by another local user.

## Out of scope

- Hermes core bugs: report them to [Hermes Agent upstream](https://github.com/NousResearch/hermes-agent/security).
- Anyone who holds a device token: by design, a token is full agent chat as its user (see the disclosure).
- The listener being reachable on your network: it is unencrypted by design. Keep it on `127.0.0.1`, Tailscale or
  behind a TLS proxy.
- How your model provider, or a cloud speech provider you configured, handles conversation content and audio.
- Third-party companion apps and firmware (the hermes-glasses iOS app, xiaozhi firmware, Omi's servers, Bobby):
  report their bugs to their own projects, unless the plugin is what breaks the boundary.

## Disclosure

This is the text the Hermes plugin catalog shows for this plugin.

Opens an unencrypted listener on all interfaces (port 8765: WebSocket `/voice`, `/xiaozhi` and
`/bobby/<token>/query`; HTTP `/turn`, `/pending`, `/approval`, `/xiaozhi/ota/<token>`, `/omi/<token>/transcript`
and `/bobby/<token>/quota`) that refuses to start without a device token (use Tailscale or a TLS proxy off your home
network; Omi's servers must reach the webhook over the internet); each device token, sent in a header, query or URL
path, gets full agent chat as its user, bypassing the gateway allowlist, including answering approval prompts and
clarify questions; `hermes glasses pair` writes device tokens to the profile `.env`, and the xiaozhi OTA reply gives
the box its WebSocket address with its own token; with Omi, transcripts come from Omi's servers and answers go back
through them; the agent can take a glasses camera photo at any time during a glasses conversation and photos are also
captured automatically on visual keywords (`auto_capture: off` disables that), saved in Hermes's image cache;
recordings and spoken replies use Hermes's own stt/tts settings, so a cloud speech provider receives the audio or
text; pushes for offline devices are kept in `$HERMES_HOME/glasses/outbox.json` (owner-only); Omi transcript
segments are held in memory for up to ten minutes per session; the device's new-session button answers Hermes's
`/new` confirmation for that user; it runs a local ffmpeg (no shell) only to convert audio; no outbound network calls
of its own and no telemetry.

## Boundaries

**Listener and tokens**
- The gateway builds the glasses channel only when a device token is configured (`GLASSES_DEVICES`, `GLASSES_TOKEN`,
  or `devices`/`token` in the platform's `config.yaml` extra). With none, it opens no port, and its three agent tools
  are not offered.
- Tokens are compared in constant time. A wrong or missing token is refused: the `/voice` and `/xiaozhi`
  WebSockets close with code `4401`, and the HTTP routes answer `401`.
- Each token maps to one `user:device`. All of a user's devices share that user's one session; no token reaches
  another user.
- Query-string tokens never reach the access log (it is off).

**Approvals**
- A spoken yes or no, or a button, answers an approval only from that user's own device, and only for the request it
  names.
- A command longer than 300 characters, or one whose spoken prompt dropped part of it, can only be denied from a
  device. Approving it needs a typed `/approve` after reading the full command.
- The plugin never answers an approval by itself.

**Photos and audio**
- Photos come only from the device in the current glasses conversation, and are saved in Hermes's own image cache.
- Recordings are transcribed, and spoken answers made, by Hermes's own `stt` and `tts` settings.
- A local `ffmpeg` runs without a shell, only to convert audio.

**Storage**
- Device tokens: the profile's `.env` (written by `hermes glasses pair`, removed by `revoke`) or `config.yaml`.
- The offline queue: `$HERMES_HOME/glasses/outbox.json`, owner-only, 50 items and 24 hours at most.
  `GLASSES_OUTBOX_PERSIST=off` keeps it in memory only.

**Network**
- The plugin makes no outbound network requests of its own and sends no telemetry.
