"""The one line a person who is not the owner gets for writing to the bot in private (docs/usage.md 基本概念): the
messages app/access.py refuses as ``not_owner``, whose sender ``Access.stranger`` names (this module compares no
open_id). At most once a person a day it says whose assistant this is and, when the deployment has one, where to get
one's own. It starts nothing: no task, no card, no model. Off: COVEY_STRANGER_REPLY=0 (the message is only logged, as
before).

- Once a day: a marker ``stranger-replies/<day>-<sha256(open_id)>`` claimed before the send (core/state.py
  ``claim_once``), the day in Asia/Shanghai as the digest's. A send that failed or may have landed is not tried again
  that day (rule 1: nothing is sent twice). Maintenance drops the markers after 7 days (core/maintenance_rules.py
  Temporary).
- What: ``line``, one plain text message at the top of that private chat. The owner's name as his login in the Bridge's
  profile gave it at the start (bot-identity.json ``owner_name``, app/unified.py init_target), else 主人; the link from
  COVEY_INVITE_URL, else from an enabled extension's ``invite_url`` setting (core/extensions.py), else none. Only an
  http(s) address counts as a link.
"""
from __future__ import annotations

import datetime
import hashlib
import re
import time

from covey.core import env as cenv
from covey.core import extensions
from covey.core.process_env import env_flag
from covey.core.state import claim_once, read_object

SWITCH = "COVEY_STRANGER_REPLY"
INVITE_ENV = "COVEY_INVITE_URL"
INVITE_SETTING = "invite_url"  # core/extensions.py SETTING_NAMES.
FOLDER = "stranger-replies"
ZONE = datetime.timezone(datetime.timedelta(hours=8))  # Asia/Shanghai, as the digest's days.
LINK = re.compile(r"https?://\S+")
NO_NAME = "主人"


def enabled() -> bool:
    return env_flag(SWITCH, True)


def invite_url() -> str:
    """Where a person gets an assistant of their own: COVEY_INVITE_URL, else an enabled extension's ``invite_url``;
    '' when the one that is set is no http(s) address."""
    url = (cenv.get(INVITE_ENV, "") or extensions.setting(INVITE_SETTING, "")).strip()
    return url if LINK.fullmatch(url) else ""


def owner_name(state_dir) -> str:
    """The owner's name as the Bridge learnt it at its start (bot-identity.json), else NO_NAME."""
    name = read_object(state_dir / "bot-identity.json").get("owner_name")
    return name.strip() if isinstance(name, str) and name.strip() else NO_NAME


def line(name, url) -> str:
    return f"这是{name}的个人助手，只处理{name}的消息。" + (f"想要一个自己的：{url}" if url else "")


def day(now=None) -> str:
    return datetime.datetime.fromtimestamp(time.time() if now is None else now, ZONE).strftime("%Y-%m-%d")


def answer(runtime, messenger, voice, ctx, event, now=None) -> bool:
    """``event`` is a message ``Access.admit`` refused: when it is a person's direct message (``Access.stranger``) and
    they were not told today, the line goes to that chat. True when it went out."""
    if not enabled():
        return False
    sender, chat_id = runtime.access.stranger(event), str(event.get("chat_id") or "")
    if not sender or not chat_id or ctx.state_dir is None:
        return False
    today, person = day(now), hashlib.sha256(sender.encode()).hexdigest()
    if not claim_once(ctx.state_dir / FOLDER, f"{today}-{person}"):
        return False
    sent = messenger.send_text(ctx.profile, chat_id, line(owner_name(ctx.state_dir), invite_url()),
                               voice.safe_key(f"stranger-{today}-{person[:16]}"))
    message = str(event.get("message_id") or event.get("id") or "")
    runtime.log(f"stranger told id={message[-10:]} sent={'yes' if sent else 'no'}")
    return bool(sent)
