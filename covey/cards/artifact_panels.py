"""The 产物 panels of a task card: Drive links and uploading state, and the older per-file send buttons.

Pure functions of the artifact paths a caller passes (app/artifact_policy.py decides which) and the upload ledger.
"""

from __future__ import annotations

from pathlib import Path

from covey.cards.elements import callback_button, markdown_panel, md
from covey.cards.review import compact_text
from covey.core.artifact_paths import artifact_key
from covey.core.text import safe_text


def _size(n):
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, round(n / 1024))} KB"


def drive_artifact_panel(paths, record, topic_id, ledger=None, *, element_id="task_artifacts", fold_uploading=False):
    """Drive links for the artifacts ``paths`` of the job ``record`` (its job_id is read only
    when the 重新上传 button is drawn). Without a finished ledger it shows the upload in progress.
    Folded once every file is up, its title naming what is in it (产物 3 个 · REPORT.md 等): open, it
    was the tallest block of a finished card (T182 #1111). Open while uploading or when something
    failed, where 重新上传 is; ``fold_uploading`` (an answer card's, app/answer_cards.py, ``element_id``
    ARTIFACTS_ID): folded while uploading too, open only when something failed."""
    ledger = ledger or {}
    status = ledger.get("status")
    files, errors = ledger.get("files", {}), ledger.get("errors", {})
    lines = []
    for raw in paths:
        key, name = artifact_key(raw), safe_text(Path(raw).name or raw, 120)
        if key in files:
            lines.append(f"- [{safe_text(files[key]['name'], 120)}]({files[key]['url']}) · {_size(files[key]['size'])}")
        elif key in errors:
            lines.append(f"- {name}：{safe_text(errors[key], 160)}")
        else:
            lines.append(f"- {name}")
    keys = [artifact_key(raw) for raw in paths]
    left = [k for k in keys if k not in files]
    failed = [k for k in left if k in errors]
    uploading = status is None or (fold_uploading and status == "uploading")
    if uploading:
        title = f"产物 {len(paths)} 个 · 上传中" if fold_uploading else f"产物 · {len(paths)} 个 · 正在上传到云空间"
        note = "上传完成后这里会换成可点击的下载链接；长时间没有变化可点“重新上传”。"
    elif status == "failed":
        title, note = f"产物 · {len(paths)} 个 · 云空间上传失败", "上传失败：" + safe_text(ledger.get("error") or "未知原因", 300)
    else:
        first = compact_text(Path(paths[0]).name or paths[0], 40) if paths else ""
        title = (f"产物 {len(paths)} 个" + (f" · {first}" if first else "") + (" 等" if len(paths) > 1 else "")
                 + (f" · {len(left)} 个未上传" if left else ""))
        note = f"[在云空间打开本任务文件夹]({ledger['folder_url']})" if ledger.get("folder_url") else ""
        if failed:
            note = "处理好未上传的文件后点“重新上传”；已上传的文件不会重复上传。" + ("\n" + note if note else "")
    panel = markdown_panel(element_id, title, "\n".join(lines), quiet=True)
    if note:
        # Its own element: a line right after the list would join the last list item.
        panel['elements'].append(md(note, size='normal_v2'))
    # An upload cut off by a restart never reports back, so the uploading state offers a retry too.
    # A file that could not be uploaded can be fixed and retried from the same card.
    retry = bool(status in (None, "failed") or left)
    panel['expanded'] = bool(status == "failed" or (left and not uploading)) if fold_uploading else retry
    if retry:
        panel['elements'].append(callback_button('重新上传', {'kind': 'bridge_artifact_upload', 'topic_id': topic_id,
                                                             'job_id': record['job_id']}))
    return panel


def artifact_panel(files, record, topic_id, page=0):
    """The older per-file send buttons for the artifacts ``files`` of the job ``record`` (its job_id is read only for a button)."""
    pages = max(1, (len(files) + 4) // 5)
    page = max(0, min(page, pages - 1))
    title = f"产物 · {len(files)} 个"
    if pages > 1:
        title += f" · 第 {page + 1}/{pages} 页"
    panel = markdown_panel("task_artifacts", title, "点击发送到本话题；同一版本不会重复发送。", quiet=True, expanded=True)
    for path in files[page*5:(page+1)*5]:
        panel['elements'].append(callback_button('发送 ' + Path(path).name[:80], {
            'kind': 'bridge_artifact_send', 'topic_id': topic_id, 'job_id': record['job_id'],
            'artifact_key': artifact_key(path)}, width='fill'))
    for label, target in [('上一页',page-1),('下一页',page+1)]:
        if 0 <= target < pages:
            panel['elements'].append(callback_button(label, {'kind': 'bridge_artifact_page', 'topic_id': topic_id,
                                                             'job_id': record['job_id'], 'page': target}))
    return panel
