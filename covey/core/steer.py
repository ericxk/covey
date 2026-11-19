"""What became of a supplement sent into a running task, and which are urgent. Standard library only:
an extension's isolated runtime imports it too."""
import re


class SteerRefused(ValueError):
    """Nothing was sent: the task cannot take a supplement now. Submitting again is safe."""


class SteerNotReady(SteerRefused):
    """Nothing was sent because the task is still starting. The process that owns the task keeps the
    supplement and sends it once the task can take it."""


class SteerUncertain(RuntimeError):
    """It may have reached the task. Never sent again automatically; ``op`` and
    ``run_dir`` let the bridge read a task runner's late acknowledgement."""

    def __init__(self, message, *, op="", run_dir=""):
        super().__init__(message)
        self.op, self.run_dir = op, str(run_dir or "")


def steer_text(text):
    """A supplement as the task receives it. Claude Code and pi run a message that starts with "/" as one of their
    commands; a supplement such as "/mnt/data/x 也看看" is text for the model."""
    return ("用户补充：" + text) if text.lstrip().startswith("/") else text


def refuse_steer(text, *, closing, ready, label, waiting=False):
    """Raise when the task cannot take ``text`` now: SteerRefused when it is empty, the task is waiting for an
    answer or is closing, SteerNotReady while it is still starting (not ``ready``). ``label`` names the Harness."""
    if not text.strip():
        raise SteerRefused("补充内容为空")
    if waiting:
        raise SteerRefused("任务正在等待回答问题，请先回答")
    if closing:
        raise SteerRefused("任务正在收尾，不能再补充；请在本轮结束后发送下一轮要求")
    if not ready:
        raise SteerNotReady(f"任务刚开始，{label} 还没准备好接收补充")


# 急的补充 (docs/architecture.md 补充投递): words that say the owner wants it taken in now, not after the step the model
# is on. Claude Code reads a supplement only between two tool calls; one sent while a long command runs waited 4 minutes
# (T538: "二维码过期，马上重发"). A supplement with one of these that is still unread URGENT_SECONDS after it was sent
# has the command the model waits on stopped (app/steer_interrupt.py); any other waits for a click.
URGENT = re.compile(r"马上|立刻|立即|立马|赶紧|赶快|紧急|急事|很急|着急|现在就|先停|停一下|停下|停掉|别跑了|别等了|不用等了|"
                    r"打断|过期|重发|搞错了|弄错了|不对了|快停|\burgent\b|\basap\b|\bstop\b|right now|immediately",
                    re.IGNORECASE)
# Commands never stopped for a supplement: each changes something outside the workspace that a run cut short could leave
# half done (a push, a deploy, a publish, an install, a remote copy, a service or cluster change, a message sent).
KEEP_RUNNING = re.compile(
    r"\bgit\b[^\n;|&]*?\s(?:push|commit|merge|rebase|cherry-pick|am|tag|reset|pull|clone|fetch)\b"
    r"|deploy|publish|release|\brollout\b|migrat"
    r"|\b(?:pip3?|uv|npm|pnpm|yarn|apt|apt-get|yum|dnf|brew|conda|gem|cargo|go)\s+(?:\S+\s+)*?(?:install|add|upgrade|remove|uninstall)\b"
    r"|\b(?:kubectl|helm|terraform|systemctl|service|launchctl|docker|podman|rsync|scp|sftp)\b"
    r"|\blark-cli\b|\bgh\s+(?:pr|release|repo|issue|api)\b|\bcurl\b[^\n|;]*\s-(?:X|-request)\s*(?:POST|PUT|PATCH|DELETE)\b",
    re.IGNORECASE)


def is_urgent(text) -> bool:
    """Whether a supplement's words ask for it to be taken in now (``URGENT``)."""
    return bool(URGENT.search(str(text or "")))


def keeps_running(command) -> bool:
    """Whether ``command`` is one a supplement never stops (``KEEP_RUNNING``)."""
    return bool(KEEP_RUNNING.search(str(command or "")))
