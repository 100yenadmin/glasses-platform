# AGENTS.md

Guidance for AI coding agents, and for people, working in this repository.

## What this is

Hermes Glasses (`glasses-platform`) is a Hermes Agent gateway platform plugin. It adds smart glasses and other
non-desktop devices as a Hermes channel. All of a person's devices share one session, and their replies, pushes,
approvals and photos travel over one authenticated listener. The plan and its status are in [ROADMAP.md](ROADMAP.md);
the wire protocol is [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Layout

- `plugin.yaml`: manifest (`kind: platform`), compatibility, settings and the disclosure.
- `__init__.py`: registration (the `glasses` platform, three agent tools, the `hermes glasses` command). It does no I/O.
- `adapter.py`: `GlassesAdapter`, which handles the listener, turns, streaming, pushes, approvals and photos.
- `protocol.py`: v0 and v1 frame parsing and building.
- `devices.py`: tokens, the device registry, authentication and the durable outbox.
- `capture.py`: photo requests and the visual-keyword heuristic. `tools.py`: the agent tools.
- `audio.py`, `ogg.py`: audio conversion (local `ffmpeg`, no shell).
- `http_api.py`, `omi.py`, `xiaozhi.py`, `pebble.py`: the HTTP route and the device bridges on the same listener.
- `cli.py`: `hermes glasses pair | devices | revoke | try`.
- `sim/`: the simulator. `conformance/`: the conformance kit for any client or server.
- `clients/`: the JavaScript and Python clients and the Even G2 app.
- `tests/`: unit tests, plus `tests/integration/` (a real gateway with a fake model, opt-in).

## Setup and checks

Tests run against a Hermes source tree (0.22.2 or later) and its virtual environment:

```bash
HERMES_SOURCE=<hermes-agent checkout> HERMES_DISABLE_LAZY_INSTALLS=1 <hermes venv>/bin/python -m pytest -q tests conformance clients/python
PATH=<hermes venv>/bin:$PATH node --test clients/js/test/ clients/even-g2/test/  # the JS tests start a Python server
<hermes venv>/bin/hermes plugins validate .
```

CI runs the Python suites and `hermes plugins validate` on Hermes v0.22.2, v0.23.0 and `main`.

## Rules

- **Fail closed.** With no device token configured the listener never starts. Never add a route that skips
  `authenticate`, and never let one user's token reach another user's session or devices.
- **No tokens anywhere but the profile's `.env` or `config.yaml`.** The one exception is `hermes glasses pair`, which
  shows the new device's connect URL and QR code once, to the person pairing it. Never log, return or otherwise print a
  token. Test fixtures build tokens at run time: no literal `token="<long value>"` strings (Hermes's install scanner
  flags them).
- **Approvals:** only a whole yes or no, or a button, from the user's own device answers one, and a clipped command
  can only be denied from a device. The plugin never answers an approval by itself.
- **Protocol changes** update [docs/PROTOCOL.md](docs/PROTOCOL.md) and the conformance kit in the same change, and
  keep v0 byte-compatible with the hermes-glasses iOS app.
- **Hermes compatibility:** feature-detect newer Hermes APIs and keep a fallback for the oldest supported version
  (`requires_hermes` in `plugin.yaml`).
- **Tests** never use the network, a real `HERMES_HOME` or a real device token. Keep timing assertions generous.
- **Branches:** branch from `next` and open the pull request against `next`. Releases go from `next` to `main`, and
  `main` always equals the latest release tag.
- **Agents:** in a change (a pull request into `next`), don't bump versions, tag, or edit the CHANGELOG release
  headings. The maintainer's release step does that: a `release/x.y.z` pull request into `main` bumps `plugin.yaml`,
  `pyproject.toml` and the CHANGELOG heading.

## Security

Report vulnerabilities privately: https://github.com/100yenadmin/glasses-platform/security/advisories/new.
See [SECURITY.md](SECURITY.md) for the scope and the boundaries.
