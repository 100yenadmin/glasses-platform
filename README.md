# glasses-platform

**Talk to your own Hermes agent from smart glasses.** Ask by voice, show it what you see, and hear or read the
answer. Reminders and long tasks can reach your glasses later.

> **Status: planning (M0).** There is no code yet. Read the [design plan](docs/PLAN.md), the
> [device protocol](docs/PROTOCOL.md) and the [roadmap](ROADMAP.md).

## What it is

A [Hermes Agent](https://hermes-agent.nousresearch.com) **gateway platform plugin** that adds smart glasses as a
channel, the same way Hermes already talks to Telegram or Slack.

- A thin companion app on your phone (or the glasses' own app platform) connects over one WebSocket.
- Hermes keeps the conversation, memory, tools, cron jobs, heartbeat and approvals.

```
glasses ─▶ companion app ─WebSocket─▶ Hermes gateway (glasses platform) ─▶ your agent
```

## Planned support

| Glasses | Path |
|---|---|
| Meta Ray-Ban (Gen 1/2), Ray-Ban Display, Oakley Meta | A phone app on Meta's Wearables Device Access Toolkit. The community [hermes-glasses](https://github.com/prasanthsasikumar/hermes-glasses) iOS app is protocol-compatible (v0). |
| Even Realities G2 | Even Hub app (M4) |
| Mentra Live | Mentra cloud app (M4) |
| Meta AI on glasses | Meta AI Connectors endpoint (M4, gated on Meta early access) |

## Why a plugin rather than a bridge script

- **Existing bridges** start a new Hermes CLI process for every utterance. They cannot reach the glasses after the
  turn ends, and they lose streaming and approvals.
- **A gateway platform** keeps one live session per person across all their devices. Cron and heartbeat results
  arrive as pushes, and replies stream to a heads-up display.

## License

MIT. See [LICENSE](LICENSE).
