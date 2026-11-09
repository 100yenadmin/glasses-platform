# Hermes Glasses for Even G2

Tap to talk to your own Hermes: G2 PCM audio goes to Hermes speech-to-text. Tap again to send (8 seconds maximum).
The transcript and streamed answer appear on the lens. Reminders and Hermes pushes wait until the turn finishes.
Approvals show Hermes's choices in a touchpad list: scroll up/down, click to answer. Another device answering closes it.
Double-tap exits. Foreground only; no speaker, camera or haptics. The phone page mirrors the lens and has settings.

## Develop and side-load

From this directory: `npm install`, then `npm run dev` (port 5173, fails if occupied).
Enable Developer Mode in the Even phone app; run `npx evenhub qr --url http://<your-lan-ip>:5173` and scan it.
For a desktop trial, run `npm run simulate` while Vite runs; no Even account is needed for the simulator.
Open settings on the phone and save the gateway WebSocket URL (path `/voice`, no query) and device token.
They stay in Even bridge local storage; tokens are never compiled into the app or logged.
Dev builds only accept `?url=<encoded-websocket-url>&token=<encoded-token>&say=<encoded-text>`;
`say` sends one text turn after connecting. Use a disposable fixture token for simulator URLs; do not share that URL.
State logs are only `[hermes] listening|thinking|answer|push|approval|error`.

## Pack for daily use

Replace `app.json` → network permission → `whitelist` with your own full gateway origin, e.g. `wss://gateway.example`.
Run `npm run pack`; it fails clearly while the placeholder remains. Side-load the resulting `.ehpk` using Even Hub's
Developer Mode package flow. Each owner packs their own origin: permissions are fixed at pack time, not by settings.
The glasses phone must reach the gateway: use Tailscale or an HTTPS reverse proxy with WebSocket support (wss).
The plugin does not terminate TLS. A token grants access to your Hermes; keep it private. No separate STT key is needed.
SDK minimum is the installed 0.0.10. Lens text shows the latest 240 characters on 576×288, updated about every 120 ms;
choices are limited to 20 and labels to 63 UTF-8 bytes. The unused template `src/asr/stt.ts` is not imported.

NOTICE: Based on the MIT-licensed `even-realities/evenhub-templates` ASR template; original template attribution retained.
