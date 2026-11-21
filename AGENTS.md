# AGENTS.md

Guidance for AI coding agents, and for people, working in this repository.

## What this is

Hermes Glasses (`glasses-platform`) is a Hermes Agent gateway platform plugin. It adds smart glasses and other
non-desktop devices as a Hermes channel. All of a person's devices share one session, and their replies, pushes,
approvals and photos travel over one authenticated listener. Its goal, users and non-goals are in [VISION.md](VISION.md);
the plan and its status are in [ROADMAP.md](ROADMAP.md);
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

CI runs the Python suites and `hermes plugins validate` on Hermes v0.22.2, v0.23.0 and `main`. The `required-ci`
ruleset on `main` requires `test (v0.22.2)` and `test (v0.23.0)`; `test (main)` runs against Hermes's moving `main`
and is not required. Renaming a matrix entry renames its check, so update the ruleset in the same change.

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

## Release and escalation
- Lane by changed path, not by plugin (below, "the orchestrating owner" and "the human owner" are this project's owners). The careful lane covers changes to storage or migrations, deletion or compaction, locking, provider routing, streaming or retries, permissions, shared hooks, dependencies or update machinery, a host-wide install (a shared plugin root or processes, including one that can't be isolated), and anything with uncertain recovery. On top of the fast-lane proof floor below, it adds mechanism tests (old-state upgrade, restart or interruption, concurrency, recovery after new writes), a consistent backup, a recovery rehearsed on a copy that keeps post-upgrade data, and a canary on one already-authorized live install, running real operations with stated pass/fail thresholds. For an install that can't be isolated, the orchestrating owner decides the blast radius. Everything else takes the fast lane.
- Fast-lane proof floor for the smallest proven fix: an independent non-author review and green CI at the exact final SHA, targeted regression tests, and a smoke test of install, startup and the affected workflow against the current release. Reaching that proof should take hours, not days. Don't wait for GA, a release date, unrelated work or a benchmark campaign. Once it passes, release on the cadence below. A remaining P0 doesn't block the other fixes if it is contained (verified workaround, no new exposure, known-issue note, named fix owner, next-update date); otherwise the orchestrating owner decides the trade-off. In either lane, before shipping, name a watcher whose wake route has been seen to fire and a ready rollback or disable command, then watch for 72 h or one update cycle. On corruption, disclosure, an unauthorized action, a critical-path failure or a breached threshold: stop, contain, roll back or disable, tell the orchestrating owner and keep the evidence. A published release can't be recalled from auto-updating users; when that matters, ship a fast corrective release with clear user instructions.
- Tests follow the diff: if the plugin has slow or benchmark suites, keep a short map from each one to the code and behavior it measures. A release runs only the suites whose inputs changed and records each skip in the release note ("suite X skipped: nothing it measures changed"). The full suite runs on its own nightly or weekly schedule, off the release path. A regression it finds gets its own fix and never gates an unrelated hotfix.
- Severity and cadence (binding release rules for maintainers; not a support promise to users). P0 is data loss, disclosure or an unauthorized action (even for one user), or the default critical workflow broken with no safe workaround: contain a suspected P0 first and tell the orchestrating owner at once. P1 is a supported workflow blocked or materially degraded for some users, without P0 harm. An owner's priority call goes into your plan and changes the order of work, never the severity. P0 fixes ship immediately; ready P1 fixes go out in at most one bundled release a day; features ride a regular train (weekly by default, set per plugin by the orchestrating owner). Never hold a ready fix past the next daily bundle, and never cut a release that carries nothing a user needs.
- Anti-stall clocks, which never reset. A P0 with no safe path within 2 h of triage goes to the orchestrating owner. For a fix candidate with no decisive test, review or authorization result within 4 h, send the orchestrating owner the blocker, the remaining evidence, the fix owner and the next result time; a longer test needs the orchestrating owner's explicit OK, once, and a missed checkpoint re-escalates. A ready fix held 4 h needs a named blocker; send it to the orchestrating owner with a deadline for their decision. Running out of time never waives proof; it forces a decision.
- Who decides: maintainers make reversible technical choices within the permissions they already have, then report. The orchestrating owner decides cross-plugin priorities, user-impact trade-offs, unresolved high risks, blast radius and longer tests; an escalation carries the evidence, the options, a recommendation, a deadline and a safe fallback. The human owner decides new access or credentials, money, anything public in their name and destructive actions. This standard grants no new authority to publish, install or spend: live installs happen only in the guarded install window (backup first, then one restart), run by whoever the orchestrating owner has assigned to it, never on your own. Silence allows only a default that is already delegated and passes the gates above; it never grants new authority or accepts new risk: on silence, hold or contain, and escalate again.

Maintainer agents: the full internal standard is in eng-standards policies/release-and-escalation.md.

## Release closeout (required after every release, rc promoted to stable, or GA tag)

After the release is published and its receipt sent:

1. Close every issue the release fully served, with a comment naming the version (never an umbrella/parent issue
   whose own acceptance is not met).
2. Tick the boxes it completed on ROADMAP.md, roadmap or tracking issues, and closed issues whose checklists shipped,
   each with its proof linked (tests or a live proof). Close a milestone once all its issues are closed and its exit
   criteria are proven.
3. Update every README / ROADMAP line that names the latest release or what ships next.
4. Post one closeout line as a comment on the release pull request: issues closed, boxes ticked, docs updated (or
   "none"). Every release here, including an rc promoted to stable or a GA tag, goes through a `release/x.y.z` pull
   request into `main`, so that pull request always exists.

## Security

Report vulnerabilities privately: https://github.com/100yenadmin/glasses-platform/security/advisories/new.
See [SECURITY.md](SECURITY.md) for the scope and the boundaries.
