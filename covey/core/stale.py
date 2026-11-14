"""Which waiting jobs and pending questions waited too long (``stale_records``), and the notes they are closed with.

Daily maintenance finds them (core/maintenance_rules.py Stale); the Bridge closes them (app/stale.py)."""

from __future__ import annotations

from covey.core.state import DAY, epoch_seconds
from covey.core.statuses import OPEN_FORMS, RUNNING, WAITING, WORKTREE_STATUS
from covey.core.store import directory_store


# Waiting jobs and pending questions whose topic saw no activity for this long are closed by the Bridge.
STALE_DAYS = 7


STALE_NOTE = f"超过 {STALE_DAYS} 天未处理，已自动关闭；需要继续请在本话题直接发消息。"


# A message whose launch card never reached Feishu (launch_delivery_error, app/launch.py) waits this
# long when its topic has no launch card to start it either: nobody can see it waiting.
UNDELIVERED_LAUNCH_DAYS = 1


UNDELIVERED_LAUNCH_NOTE = f"启动卡未送达，超过 {UNDELIVERED_LAUNCH_DAYS} 天未处理，已自动关闭；需要继续请在本话题直接发消息。"


# The questions the stale closer cancels: an `uncertain` answer (delivery unconfirmed) is not among them,
# its card keeps 核查已保存回答并继续. Not core/statuses.OPEN_QUESTIONS, which also counts answers on their way.
STALE_QUESTION_STATES = frozenset({"pending"})


# Besides its job records (its messages), the records whose change is someone using the topic:
# answers, held notes, 补充 messages, launch-card and 换配置 form edits, opened task records.
ACTIVITY_DIRS = ("questions", "held-notes", "interaction-deliveries", "launch-forms", "switch-forms", "browse-cards")


def _when(value):
    """A record time (epoch seconds or ISO 8601) as epoch seconds; inf when it cannot be read,
    so an undatable record never looks old."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    seconds = epoch_seconds(str(value))
    return float("inf") if seconds is None else seconds


def topic_activity(state, records):
    """When each topic (topic_id) was last used: the last change of any of its job records or of
    its other records in ACTIVITY_DIRS."""
    latest = {}

    def seen(topic, when):
        if topic:
            latest[topic] = max(latest.get(topic, 0.0), when)

    for record in records:
        seen(record.get("topic_id"), max(_when(record.get("updated_at", 0)), _when(record.get("created_at", 0))))
    for name in ACTIVITY_DIRS:
        folder = state / name
        for path, record in directory_store(folder).query(where=lambda r: bool(r.get("topic_id"))) \
                if folder.is_dir() else []:
            try:
                seen(record["topic_id"], path.stat().st_mtime)
            except OSError:
                continue
    return latest


def stale_records(state, records, live, now, days=STALE_DAYS):
    """Waiting jobs (job keys) and pending questions (tokens) with no activity for ``days``, and
    since when (``since``: {"jobs": {key: time}, "questions": {token: time}}).

    Activity is the newest of the record's own change, its job's or its pending questions', and
    any use of its topic (``topic_activity``). Never stale: a job with a live runner, the
    questions of a job that runs, and an answer whose delivery is unconfirmed (``uncertain``)
    with its waiting job, which wait for 核查已保存回答并继续 or the next message."""
    jobs = {r["key"]: r for r in records if r.get("key")}
    folder = state / "questions"
    questions = [q for _, q in directory_store(folder).query()] if folder.is_dir() else []
    open_questions = [q for q in questions if q.get("status") in STALE_QUESTION_STATES and q.get("token")]
    unconfirmed = {q.get("job_key") for q in questions if q.get("status") == "uncertain"}
    topics = topic_activity(state, records)
    cutoff = now - days * DAY
    startable, failed = _launch_forms(state, now)
    stale = {"jobs": [], "questions": [], "undelivered": [], "since": {"jobs": {}, "questions": {}}}
    for question in open_questions:
        job = jobs.get(question.get("job_key")) or {}
        if question.get("job_key") in live or job.get("status") in RUNNING:
            continue
        since = max(_when(question.get("created_at")), _when(job.get("updated_at", 0)),
                    topics.get(question.get("topic_id") or job.get("topic_id"), 0.0))
        if since < cutoff:
            stale["questions"].append(question["token"])
            stale["since"]["questions"][question["token"]] = since
    for key, record in jobs.items():
        if record.get("status") not in WAITING or key in live or key in unconfirmed:
            continue
        times = [_when(record.get("updated_at")), _when(record.get("created_at", 0)), topics.get(record.get("topic_id"), 0.0)]
        times += [_when(q.get("created_at")) for q in open_questions if q.get("job_key") == key]
        where = (record.get("topic_id"), record.get("chat_id"))
        # Undelivered: its own card failed (launch_delivery_error, or a release before it left only the
        # form's delivery_failed, live #1774), and no card in the topic can start it.
        undelivered = (record.get("status") == "waiting_workspace" and where not in startable
                       and (bool(record.get("launch_delivery_error")) or where in failed))
        if max(times) < (max(cutoff, now - UNDELIVERED_LAUNCH_DAYS * DAY) if undelivered else cutoff):
            stale["jobs"].append(key)
            stale["since"]["jobs"][key] = max(times)
            if undelivered:
                stale["undelivered"].append(key)
    return stale


def _launch_forms(state, now):
    """(topic_id, chat_id) pairs with a launch form that can start their waiting messages (open,
    creating its worktree, or publishing and written within a day: older is a publish a restart cut
    off), and those with a form whose card was not delivered."""
    folder = state / "launch-forms"
    startable, failed = set(), set()
    found = directory_store(folder).query(
        where=lambda r: r.get("status") in OPEN_FORMS | {WORKTREE_STATUS, "publishing", "delivery_failed"}) \
        if folder.is_dir() else []
    for path, form in found:
        where, status = (form.get("topic_id"), form.get("chat_id")), form.get("status")
        if status == "delivery_failed":
            failed.add(where)
            continue
        if status == "publishing":
            try:
                if path.stat().st_mtime < now - DAY:
                    continue
            except OSError:
                continue
        startable.add(where)
    return startable, failed
