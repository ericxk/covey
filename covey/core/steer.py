"""What became of a supplement sent into a running task. Standard library only:
an extension's isolated runtime imports it too."""


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
