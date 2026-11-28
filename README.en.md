<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/media/brand/hero-en-dark.svg">
    <img src="docs/media/brand/hero-en-light.svg" width="860" alt="Covey: run a small team of coding agents from Feishu">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/ericxk/covey/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/ericxk/covey/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green.svg"></a>
  <a href="pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue.svg"></a>
  <a href="https://github.com/ericxk/covey/tags"><img alt="version" src="https://img.shields.io/github/v/tag/ericxk/covey?label=version"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="https://ericxk.github.io/covey/">Live demo</a> ·
  <a href="#documentation">Documentation</a> ·
  <a href="README.md">中文</a>
</p>

Say one sentence in a private chat with your bot. The butler hands the work to coding agents such as Claude Code, Codex and Antigravity, running on your own development machine. Every job gets a topic and a session of its own, with progress and results on cards. Restarts and rate limits don't lose the work, and you hear from it only when a decision is yours.

Covey is built for Feishu, the Chinese edition of Lark (the installer sets up a Feishu app), for people who keep several coding agents busy at once. Most of the documentation is in Chinese for now.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/media/demo/flow-en-dark.gif">
    <img src="docs/media/demo/flow-en-light.gif" width="640" alt="Demo: one sentence in the private chat, the butler opens a topic, a multiple-choice question is answered, and the answer arrives with its artifacts">
  </picture>
</p>

<sub>Card sketches drawn from Covey's card JSON with demo data, not screenshots of the real client; Feishu draws the real thing. The interface is in Chinese. To click through it yourself: the [live demo](https://ericxk.github.io/covey/), or open the [three-step demo page](docs/demo/index.html) in a browser from a checkout (hand off to a topic → answer a question → see the answer and its artifacts).</sub>


## Quick start

Requirements: Linux or macOS, Python 3.10+, Node.js and npm (`./install` installs `lark-cli` with npm when it is missing; without npm it stops and tells you to install Node.js), one of Claude Code, Codex or Antigravity installed and logged in on this machine, and a Feishu account (you scan a QR code with the phone app).

```bash
git clone https://github.com/ericxk/covey.git && cd covey
./install        # scan the QR code in the terminal once with the Feishu mobile app
```

The installer creates the Feishu app, adds its permissions, events and menu, registers the background service, and finishes once your first message in the private chat has been answered. Common options:

| Option | What it does |
| --- | --- |
| `--showcase` | The showcase set: on top of the defaults, collaboration between topics (in development), the pinned "on your plate" card and topic pins, daily digests, parallel tracks for the butler, dispatch cards |
| `--web` | Self-host the web workbench on this machine (needs Node 22+ and npm) |
| `--web-public ADDR` | Also serve the web workbench over HTTPS on a public IP or domain (implies `--web`) |

No Claude Code? It installs anyway: the butler runs on whichever of Codex or Antigravity answered, and projects stay on ([install, without Claude Code](docs/install.md#没有-claude-code-时)). All options and troubleshooting: [install](docs/install.md).

## Say it once, then stop babysitting

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/media/brand/workflow-en-dark.svg">
    <img src="docs/media/brand/workflow-en-light.svg" width="860" alt="Workflow: one sentence in the private chat, the butler opens topics, each runs in its own session, a question card asks only when the call is yours, and the conclusion comes back to the chat">
  </picture>
</p>

You say one sentence → the butler hands it out → topics do the work, and a project lead follows the long ones → a question card when the call is yours → the conclusion comes back to the chat.

Once several agents run at the same time, the bottleneck is rarely the model. It is you: opening terminals, picking directories, remembering which session does what, copying context between them, and checking back to see whether one stopped. Covey takes that over.

- **Say it once.** In the private chat you talk to the butler only. It answers what it can right away. For work that needs reading or changing code, it opens a topic, which hangs under your message. Each topic is its own session, with its own harness, model and workspace. When the work is done the conclusion comes back to the chat, and topics that finish close together arrive as one card.
- **Hand it off and walk away.** Every task runs in a process of its own, so restarting or upgrading the bot does not stop it. After a rate limit or a passing fault of the model service, it waits until it can go on and carries on by itself, for up to about three hours. A topic waiting on CI or a training run sets a time to come back and look, and stays quiet when nothing has changed.
- **It asks only when the call is yours.** A decision comes as a question card: tap an option and it is sent. There is no default answer, and no answer is never taken as a yes. The board (看板) in the bot's menu shows on one card what waits for you, what stopped, which results you have not seen and what is running.
- **Long work has someone in charge.** Work that spans days and several topics becomes a project. A lead topic splits it up, opens topics, follows them and reports. The lead sees the topics' questions first, and when a project stops it asks you whether to go on, wrap up or stop. As topics pile up, project tidying proposes a project; nothing is built until you click.
- **Memory that knows who said what.** Standing instructions, what you asked it to "remember" and what a project decided form one memory that travels with every turn. Each entry says what it covers and whether you said it or the agent summed it up; one tap on Wrong (不对) under an answer takes a wrong one back.
- **Switch models mid-thread.** One bot connects Claude Code, Codex, Antigravity and DeepSeek Harness (dsh); the butler and project leads run on any of the first three, so you are not tied to one vendor. Halfway through a topic you can change its harness, model or workspace and keep talking.
- **Inside Feishu, and safe to bring to work.** No extra app to install: Feishu on your phone, desktop or the web is the client, and installing takes one QR scan. The bot serves you alone. A group is enabled only after you confirm it in the private chat, and group members can be limited to read-only. Sensitive terms are swapped for stand-ins before a request leaves the machine (Claude models), and an official refusal stops the turn instead of retrying around it.

### How it differs

- **Chat remote controls** turn a message into one agent call; a chat is roughly one session, and when the session closes the work is gone. Covey handles what sits above that: who does the work, how far it got, who is waiting for you, and whether the result was delivered.
- **Agent IDEs and desktop workbenches** live in the editor at your desk, with the phone and chat as extensions. Covey does not compete with your editor; it keeps watch while you are away from it.
- **All-purpose personal assistants** cover a lot of ground and need their own client apps. Covey does one thing, coding agents, in the Feishu your team already uses.

## The cards

<table>
<tr>
<td width="33%"><img src="docs/media/screenshots/02-task-card.png" width="100%" alt="Task card: status, input and output, the execution trace; steer, switch config or stop"><br><sub>Task card: a live execution trace; steer, switch config or stop at any time</sub></td>
<td width="33%"><img src="docs/media/screenshots/06-question-card.png" width="100%" alt="Question card: options as buttons, one tap sends it; write your own or skip the question"><br><sub>Question card: one tap sends an option; no default answer</sub></td>
<td width="33%"><img src="docs/media/screenshots/03-answer-card.png" width="100%" alt="Answer card: the conclusion, artifacts and changes, input and output"><br><sub>Answer card: the conclusion, artifacts and changes, input and output</sub></td>
</tr>
</table>

<details>
<summary>Five more: stopped, switch config, the board, launch card, redaction</summary>

<table>
<tr>
<td width="50%"><img src="docs/media/screenshots/07-stopped-card.png" width="100%" alt="A stopped relay card: the reason and the next step in plain words, with a switch model button"><br><sub>Stopped: why and what next, in plain words; out of quota, switch the model</sub></td>
<td width="50%"><img src="docs/media/screenshots/08-switch-card.png" width="100%" alt="Switch config card: from Claude Code to Codex, the changed rows in blue"><br><sub>Switch config: change harness, model or workspace mid-conversation</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/media/screenshots/09-board.png" width="100%" alt="The board: how many things need you, waiting, stopped, results, projects"><br><sub>The board: what needs you, and what to look at first</sub></td>
<td width="50%"><img src="docs/media/screenshots/04-launch-card.png" width="100%" alt="Launch card: model, thinking budget, redaction, workspace and run mode"><br><sub>Launch card: without a butler, each new topic picks its model and workspace first</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/media/screenshots/05-settings-redaction.png" width="100%" alt="The redaction tab of the settings card: type the terms, stand-ins are generated"><br><sub>Settings, redaction tab: type the terms, stand-ins are generated</sub></td>
<td width="50%"></td>
</tr>
</table>

</details>

## Web workbench (open source, optional)

Better for long answers and history on a computer: the projects, topics, turns and pending questions of your private chat are all on the page, where you can also talk, answer, stop a topic, start a new one and change settings. It reads and writes the same records as Feishu, which keeps the notifications and quick replies.

<img src="docs/media/screenshots/10-web-board.png" width="100%" alt="Web board: two things need you; waiting, stopped, running and unseen results; projects and weekly usage on the right">

<details>
<summary>Project page, the room, on a phone</summary>

<table>
<tr>
<td width="40%"><img src="docs/media/screenshots/11-web-project.png" width="100%" alt="Web project page: the conversation with the lead; on the right the memory, marked as said by you or summed up by the agent"><br><sub>Project page: the lead's conversation; memory split into "you said" and "summed up"</sub></td>
<td width="40%"><img src="docs/media/screenshots/12-web-hall.png" width="100%" alt="Web room: you, the butler and the leads on one timeline"><br><sub>The room: you, the butler and every project's lead on one timeline</sub></td>
<td width="20%"><img src="docs/media/screenshots/13-web-phone.png" width="100%" alt="The web board on a phone"><br><sub>On a phone</sub></td>
</tr>
</table>

</details>

<sub>Web screenshots: the self-hosted version filled with demo data, no real data.</sub>

The web workbench source is in [`web/`](web/README.md), with two ways to deploy it:

- **One-step deploy to Feishu's full-stack app platform**: if your Feishu organisation has it, one command deploys the page as an app of your own; the platform provides the address, TLS and Feishu login, so you can open it from the Feishu app on your phone anywhere. See [Deploy in web/README.md](web/README.md#部署).
- **No such platform, or you want the data to stay on your machine**: `./install --web` self-hosts it on this machine; `./install --web-public <IP or domain>` also serves it over HTTPS on a public address, so a cloud server or a machine at home with a public address can be reached from outside. The first login is approved with one tap in your Feishu private chat. See [docs/install.md](docs/install.md#网页工作台可选) and [web/deploy/selfhost](web/deploy/selfhost/README.md).

## After installing

1. **First message**: write to the bot in a private chat; the butler answers or opens a topic. One page of how to use it: [five-minute start](docs/quickstart.md); then walk through [first use](docs/usage.md#第一次使用).
2. **On by default**: the butler, projects, project tidying (proposals only), the board (看板) menu item, revisits, memory and knowledge entries. Recalling or editing a message stops or updates its task, and what you write in a topic while its task runs goes straight into it. What else can be turned on and how: [switches at a glance](docs/configuration.md#开关速查); to turn the whole showcase set on in a running bot, see [the showcase set](docs/configuration.md#演示套餐).
3. **Upgrade**: run `scripts/upgrade.sh` in the checkout (`--dry-run` first to see what it would do). It takes the new upstream commit and deploys it as a fixed release with the service's own settings; the bot restarts in a few seconds, and running tasks go on as the new process takes them over. Do not `git pull` in the checkout the service runs from. See [daily upgrades](docs/deploy.md#日常升级).

## Documentation

All in Chinese.

- **Getting started and use**: [install](docs/install.md), [five-minute start](docs/quickstart.md) (one page; the butler reads it when asked how to use Covey), [usage](docs/usage.md), [usage reference](docs/reference.md), [butler](docs/butler.md), [projects](docs/projects.md), [web workbench](docs/web.md), [proxy replies](docs/proxy.md), [troubleshooting](docs/troubleshooting.md).
- **Deployment and configuration**: [configuration](docs/configuration.md), [harnesses](docs/harnesses.md), [operations](docs/operations.md), [upgrade and deploy](docs/deploy.md), [Feishu app configuration](docs/feishu-app.md), [manual install](docs/manual-install.md), [renaming and compatibility](docs/compat.md), [retired features and old configuration](docs/legacy.md), [policy stops](docs/policy-stop.md), [runtime locks](docs/runtime-locks.md).
- **Development**: [architecture](docs/architecture.md), [butler internals](docs/butler-internals.md), [cards](docs/cards.md), [UI](docs/ui.md), [testing](docs/testing.md), [glossary](docs/glossary.md), [AGENTS.md](AGENTS.md) (code map and the rules most easily broken), [CONTRIBUTING.md](CONTRIBUTING.md).

## Extensions

The core refers to nothing but the built-in harnesses. An extension is a package with a manifest (its `manifest` module) under `extensions/` of the code that runs (a fixed release gets it from `scripts/deploy_bridge.sh --ext-repo`); its manifest merges harnesses, providers, model gateways, menu items and hooks into the core's tables. Once it is there, `python3 -B bin/covey_plugins.py list`, `enable <package>` and `disable <package>` manage it, and `covey_service.py restart` puts a change into effect; see [operations](docs/operations.md#扩展的装和卸) and [architecture](docs/architecture.md#扩展). An extension's own install steps come with it.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). License: [MIT](LICENSE).
