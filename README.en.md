# Covey

[中文](README.md)

Covey drives the coding agents on your own development machine from Feishu (Lark) chat. One bot connects Codex, Claude Code, DeepSeek Harness (dsh) and Pi; one Feishu topic is one agent session, and the task, follow-ups, steering, questions and results all stay in that topic. More harnesses can be added through [extensions](docs/architecture.md#扩展).

The bot runs on your own machine, serves exactly one bound user and uses that user's own model authorization. A colleague who wants it deploys their own.

Most of the documentation is written in Chinese for now. Screenshots and a demo video will be added later, recorded with demo data.

## What it does

- **A topic is a session.** Send a message to open a topic, pick model, thinking budget, workspace and run mode, and start; later messages in the topic continue the same session.
- **Private-chat main thread.** What you write at the top level of the private chat goes to one resident Claude Code session. It answers quick questions itself and opens topics for real work; the conclusions come back to the chat.
- **Work on the card.** Watch the execution trace, stop a task, steer it while it runs, answer the model's questions, switch harness or model mid-conversation; Claude topics can also step back one turn.
- **Results and artifacts.** The answer is sent as its own card; registered artifacts are uploaded to cloud storage with a link.
- **Long tasks survive restarts.** Each task runs in its own process, so restarting or upgrading the bot does not stop it.
- **Spaces.** Group topics by purpose, each with its own default workspace and settings.
- **Optional features**, off by default: a status board and daily digests, proxy replies, sensitive-word redaction. See [configuration](docs/configuration.md).

## Quick start

Requirements: Claude Code or Codex installed and logged in on this machine, Linux or macOS, Python 3.10+.

```bash
git clone https://github.com/OWNER/covey.git && cd covey
./install        # scan the QR code in the terminal once with the Feishu mobile app
```

Then send the bot a message in a private chat. Options and troubleshooting: [docs/install.md](docs/install.md).

## Documentation

All in Chinese: [install](docs/install.md), [usage](docs/usage.md), [configuration](docs/configuration.md), [operations](docs/operations.md), [troubleshooting](docs/troubleshooting.md), [harnesses](docs/harnesses.md), [architecture](docs/architecture.md), [testing](docs/testing.md), and [AGENTS.md](AGENTS.md) (code map and the rules most easily broken).

## Extensions

The core refers to nothing but the built-in harnesses. A deployment can put extension packages under `extensions/` and enable them with `extensions/enabled` or the `COVEY_EXTENSIONS` environment variable; an extension merges its harnesses, providers and menu items into the core's tables through its manifest. See the extension section of [architecture](docs/architecture.md#扩展).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). License: [MIT](LICENSE).
