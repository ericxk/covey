"""The ↩️ 回退 button and card (app/rewind.py keeps the card's record and takes its clicks)."""
from __future__ import annotations

from pathlib import Path

from covey.cards.elements import (callback, callback_button, md, note, panel_header, row, styled_panel,
                                           topic_button)
from covey.core.text import safe_text

OPEN = "bridge_rewind_open"
CONFIRM = "bridge_rewind_confirm"  # Its value's "files": the turn's files go back too.
CANCEL = "bridge_rewind_cancel"
UNDO = "bridge_rewind_undo"
KINDS = frozenset({OPEN, CONFIRM, CANCEL, UNDO})
WHAT = "回退后，下一条消息接在上一轮后面：这一轮的提问和回答 Claude 不会再看到，话题记录里也去掉。"
# A turn that began its session has no turn before it there: the next message starts a new session, which
# reads the topic's history without this turn (app/rewind.py clears the session pointer).
WHAT_FRESH = "这一轮是当前会话的第一轮：回退后，下一条消息开新会话，这一轮的提问和回答从话题记录里去掉。"
FILES_NOTE = ("只包括 Claude 用编辑工具改的文件；命令改的文件和 git 提交不会动。原目录运行时，"
              "别的话题之后对这些文件的改动也会一起改回。")
NO_CHECKPOINT = "这一轮没有文件快照（回退功能上线前运行的），只能回退对话；它改过的文件要手动处理。"
NO_CHANGES = "这一轮没有用编辑工具改文件，文件不用动。"
UNDO_NOTE = "发下一条消息之前，可以撤销这次回退。"


def rewind_button(topic_id, job_id, harness_id, revision):
    return topic_button("↩️ 回退", element_id="rewind_action", behaviors=callback(
        {"kind": OPEN, "topic_id": topic_id, "job_id": job_id, "harness_id": harness_id, "binding_revision": revision}))


def _shown(path, workdir):
    """A file under the workdir relative to it, one elsewhere by its last two parts (a full absolute path took
    six lines of the topic side panel, the 2026-11-03 client check)."""
    try:
        return str(Path(path).relative_to(workdir))
    except ValueError:
        parts = Path(path).parts
        return "…/" + "/".join(parts[-2:]) if len(parts) > 3 else str(path)


def file_lines(point, workdir, actions=True):
    """The files a file rewind would change, a line each (with what it would do), and how many more."""
    lines = [f"- `{_shown(path, workdir)}`" + (("：删除（这一轮新建的）" if action == "delete" else "：改回这一轮之前")
                                               if actions else "")
             for path, action in point.get("files") or ()]
    more = int(point.get("file_count") or 0) - len(lines)
    return "\n".join(lines + ([f"- 另有 {more} 个"] if more > 0 else []))


def subtitle(form):
    return (f"话题 {form['topic']} · " if form.get("topic") else "") + f"任务 #{form['job_id']}"


def rewind_card(form):
    """The card a 回退 click opens: the turn it drops, the files it could put back, and the choices."""
    token, point = form["token"], form["point"]
    count = int(point.get("file_count") or 0)
    elements = [md(f"**要去掉的这一轮**：{safe_text(form.get('user_text') or '（没有文字）', 120, markdown=True)}"),
                note(WHAT if point.get("keep") else WHAT_FRESH)]
    if not point.get("checkpoint"):
        elements.append(note(NO_CHECKPOINT))
    elif not count:
        elements.append(note(NO_CHANGES))
    else:
        elements += [md(f"**这一轮改过的文件**（{count} 个）\n" + file_lines(point, form.get("workdir") or "")),
                     note(FILES_NOTE)]
    if form.get("notice"):
        elements.append(md(form["notice"]))

    def button(label, files, style):
        return callback_button(label, {"kind": CONFIRM, "token": token, "files": files}, style=style, width="fill"), 1
    cancel = (callback_button("取消", {"kind": CANCEL, "token": token}, style="default", width="fill"), 1)
    if count:
        elements.append(row(button("对话和文件都回退", True, "primary_filled"), button("只回退对话", False, "default"),
                            cancel))
    else:
        elements.append(row(button("回退这一轮", False, "primary_filled"), cancel))
    return styled_panel(panel_header("rewind", "回退一步", subtitle(form)), elements, "回退一步 · 去掉最新一轮")


def done_card(form):
    """The card once the turn is gone: what went, the files, and 撤销回退 while the conversation alone went."""
    done = form["done"]
    elements = [md(f"已去掉这一轮：{safe_text(form.get('user_text') or '（没有文字）', 120, markdown=True)}\n"
                   + ("下一条消息接在上一轮后面。" if form["point"].get("keep") else "下一条消息开新会话。"))]
    if done.get("files"):
        elements.append(md(f"已把 {done['files']} 个文件改回这一轮开始时的样子。"))
    elif int(form["point"].get("file_count") or 0):
        # The list in its own element: the client runs a line after a list item into it, and the files joined
        # on one line with what a file rewind would have done read as done (the 2026-11-03 client check).
        elements += [note("这些文件没有改回，仍是这一轮改过的样子："),
                     md(file_lines(form["point"], form.get("workdir") or "", actions=False), size="notation")]
    if form.get("notice"):
        elements.append(md(form["notice"]))
    if not done.get("files"):
        elements += [note(UNDO_NOTE), row((callback_button("撤销回退", {"kind": UNDO, "token": form["token"]},
                                                           style="default", width="fill"), 1))]
    return styled_panel(panel_header("submitted", "已回退一步", subtitle(form), color="green"), elements,
                        "已回退一步")
