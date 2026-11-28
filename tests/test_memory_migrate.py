"""The memory's migration (core/migrate.py memory_v2, docs/operations.md 记忆的存储与迁移): an older state taken whole and
once, its files left byte for byte; a rollback (an older release writing its own files) and an upgrade again losing no
new field, code or status; the lead's lines the turns carried kept rules and put in 待审核; no topic directory read at
start; and the way back for an older release (export_legacy) read again without doubles."""
from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from support.records import state_dir

from covey.core import knowledge, migrate, memstore, principles, project_memory, projects, topic_memory

CHAT = "oc_owner_dm"
PID = "a1b2c3d4e5f6"
OTHER = "b1b2c3d4e5f6"
TID = "e" * 64
NOW = 1_800_000_000.0
DAY = 86400


def digest(state) -> dict:
    """path -> sha1 of every file an older release keeps of the memory."""
    out = {}
    for path in sorted(Path(state).rglob("*")):
        rel = path.relative_to(state).as_posix()
        if path.is_file() and not rel.startswith(("memory/", "backups/")) and ".locks" not in rel \
                and not rel.endswith(("memory-v2.json", "migrations.json")):
            out[rel] = hashlib.sha1(path.read_bytes()).hexdigest()
    return out


class Case(unittest.TestCase):
    def setUp(self):
        self.state = state_dir(self)
        memstore._cache.clear()
        s = self.state
        for pid in (PID, OTHER):
            projects.create(s, projects.new_record(chat_id=CHAT, name=f"专项 {pid[:2]}", project_id=pid, now=NOW))
        self.p1, _ = principles.save(s, principles.OWNER, "回复主人用中文")
        self.p2, _ = principles.save(s, principles.OWNER, "开工前先查看当前状态")
        self.p3, _ = principles.save(s, principles.OWNER, "写文档只写实际有的数据")
        principles.save(s, principles.WORKSPACE, "这个仓库用 pytest", workspace="app")
        self.k1, _ = knowledge.save(s, knowledge.OWNER, "decision", "部署前先跑预检")
        self.k2, _ = knowledge.save(s, knowledge.OWNER, "fact", "演示网页在 8080")
        old, _ = knowledge.save(s, knowledge.OWNER, "pitfall", "老做法")
        knowledge.update(s, old["id"], "新做法", reason="改了")
        knowledge.save(s, knowledge.WORKSPACE, "practice", "构建前装依赖", workspace="app")
        self.owner_line, _ = project_memory.add(s, PID, "同一账号只对应一份额度", "constraint", "owner", now=NOW)
        self.carried_line, _ = project_memory.add(s, PID, "同事机器只读不改", "constraint", "lead", now=NOW)
        self.idle_line, _ = project_memory.add(s, PID, "旧的约束没人带", "constraint", "lead", now=NOW)
        self.lead_decision, _ = project_memory.add(s, PID, "分层：本机多账号常见", "decision", "lead", now=NOW)
        self.fact, _ = project_memory.add(s, PID, "包 B 已合入", "fact", "lead", now=NOW)
        self.confirmed, _ = project_memory.add(s, PID, "并行改要用 worktree", "constraint", "lead", now=NOW)
        project_memory.fix(s, PID, self.confirmed["id"], "confirm", origin={"message_id": "om_1"}, now=NOW)
        project_memory.set_style(s, PID, "汇报先给结论")
        record = {"token": "0123456789abcdef", "topic_id": TID, "job_id": 3, "questions": [], "answers": {}}
        project_memory.note_answers(s, PID, record, [{"index": 1, "item": "x", "text": "主人答：stable 什么时候推？→跑满 12 小时"}],
                                    now=NOW)
        topic_memory.add(s, TID, "这个话题只读", now=NOW)
        (Path(s) / "memory-candidates.json").write_text(json.dumps({"schema_version": 1, "rows": [
            {"id": "c1", "text": "汇报先给结论再给表", "state": "open", "at": NOW},
            {"id": "c2", "text": "不要的", "state": "declined", "at": NOW}]}, ensure_ascii=False))
        jobs = Path(s, "jobs")
        jobs.mkdir()
        carried = [{"n": "M12", "ref": f"project:{PID}:{self.carried_line['id']}", "scope": "project", "by": "lead"},
                   {"n": "M13", "ref": f"project:{PID}:{self.lead_decision['id']}", "scope": "project", "by": "lead"}]
        (jobs / "j1.json").write_text(json.dumps({"key": "j1", "memory_ids": carried}))
        stale = {"key": "j0", "memory_ids": [{"n": "M14", "ref": f"project:{PID}:{self.idle_line['id']}"}]}
        (jobs / "j0.json").write_text(json.dumps(stale))
        os.utime(jobs / "j0.json", (NOW - 30 * DAY, NOW - 30 * DAY))
        os.utime(jobs / "j1.json", (NOW - DAY, NOW - DAY))
        Path(s, "topics", TID, "topic.json").write_text("{}")
        memstore.topics_path(s).unlink()  # An older release kept no index.

    def run_step(self, now=NOW):
        with mock.patch("time.time", return_value=now):
            return migrate.memory_v2(self.state, now=now)

    def by_text(self, **kwargs):
        return {e.text: e for e in memstore.entries(self.state, statuses=None, **kwargs)}


class MigrateTest(Case):
    def test_everything_is_taken_once_and_the_old_files_stay_byte_for_byte(self):
        before = digest(self.state)
        written, failed = self.run_step()
        self.assertEqual(failed, [])
        self.assertGreater(written, 0)
        self.assertEqual(digest(self.state), before)
        backups = list(Path(self.state, "backups").glob("memory-v2-*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "principles.json").is_file())
        self.assertTrue((backups[0] / "projects" / PID / "memory.json").is_file())
        glob = self.by_text(scope="global")
        self.assertEqual({t: (e.kind, e.status) for t, e in glob.items()},
                         {"回复主人用中文": ("rule", "active"), "开工前先查看当前状态": ("rule", "active"),
                          "写文档只写实际有的数据": ("rule", "active"), "部署前先跑预检": ("rule", "active"),
                          "演示网页在 8080": ("note", "active"), "老做法": ("note", "archived"), "新做法": ("note", "active")})
        self.assertEqual(glob["老做法"].superseded_by, glob["新做法"].ref)
        self.assertEqual(glob["回复主人用中文"].aliases, (f"principle:{self.p1['id']}",))
        self.assertEqual(memstore.find(self.state, self.p1["id"]), glob["回复主人用中文"])
        mine = self.by_text(scope="project", owner_key=PID)
        self.assertEqual({t: (e.kind, e.said) for t, e in mine.items()}, {
            "同一账号只对应一份额度": ("rule", "owner"), "同事机器只读不改": ("note", "summary"),
            "旧的约束没人带": ("note", "summary"), "分层：本机多账号常见": ("note", "summary"), "包 B 已合入": ("note", "summary"),
            "并行改要用 worktree": ("rule", "owner"), "汇报先给结论": ("rule", "owner"),
            "主人答：stable 什么时候推？→跑满 12 小时": ("decision", "owner")})
        review = memstore.review_items(self.state)
        # T914 M1: a lead's line and an evening candidate put nothing in 待审核; the workspace groups still do.
        self.assertEqual(sorted((item["type"], item["text"]) for item in review), [
            ("migrate", "构建前装依赖"), ("migrate", "这个仓库用 pytest")])
        self.assertTrue(all(memstore.is_code(e.code) for e in memstore.entries(self.state, statuses=None)))
        # Again: nothing more, nothing doubled.
        codes = {e.ref: e.code for e in memstore.entries(self.state, statuses=None)}
        self.assertEqual(self.run_step(NOW + 60), (0, []))
        self.assertEqual({e.ref: e.code for e in memstore.entries(self.state, statuses=None)}, codes)
        self.assertEqual(len(memstore.review_items(self.state)), 2)
        self.assertEqual(migrate._memory_left(self.state), 0)

    def test_a_lead_line_an_earlier_run_kept_a_rule_stays_one_and_none_goes_to_review(self):
        memstore._put_sidecar(self.state, "project", PID, {self.carried_line["id"]: {  # What an earlier release's guard left.
            "kind": "rule", "said": "summary", "by": "lead", "guard": {"at": NOW - DAY, "kept": True, "review": "review:migrate:x"}}})
        self.run_step()
        mine = self.by_text(scope="project", owner_key=PID)
        self.assertEqual((mine["同事机器只读不改"].kind, mine["同事机器只读不改"].said), ("rule", "summary"))
        self.assertEqual(mine["分层：本机多账号常见"].kind, "note")
        self.assertFalse([item for item in memstore.review_items(self.state) if item["suggest"].get("scope") == "project"])
        self.assertFalse(hasattr(memstore, "guard_project"))

    def test_a_rollback_and_an_upgrade_again_lose_nothing(self):
        self.run_step()
        glob = self.by_text(scope="global")
        retitled = memstore.edit(self.state, glob["回复主人用中文"].ref, title="中文回复", now=NOW + 1)
        archived = memstore.archive(self.state, glob["写文档只写实际有的数据"].ref, now=NOW + 1)
        mine = self.by_text(scope="project", owner_key=PID)
        noted = memstore.edit(self.state, mine["包 B 已合入"].ref, title="包 B", repos=["covey"], now=NOW + 1)
        topic_entry = memstore.entries(self.state, scope="topic", owner_key=TID)[0]
        new_global = memstore.add(self.state, kind="rule", scope="global", text="只有新版才有的规矩", now=NOW + 5)
        codes = {e.ref: e.code for e in memstore.entries(self.state, statuses=None)}
        codes.update({topic_entry.ref: topic_entry.code})
        # The older release runs: it writes its own files with its own keys only.
        later = NOW + 3600
        principles.save(self.state, principles.OWNER, "回滚期间新记的原则")
        principles.edit(self.state, self.p2["id"], "开工前先查看当前状态再动手")
        principles.edit(self.state, self.p1["id"], "回复主人一律用中文")  # Its title was changed here: kept, offered.
        principles.delete(self.state, self.p3["id"])
        knowledge.save(self.state, knowledge.OWNER, "practice", "回滚期间记的做法")
        added, _ = project_memory.add(self.state, PID, "回滚期间负责人记的", "constraint", "lead", now=later)
        project_memory.settle(self.state, PID, [{"id": self.fact["id"], "valid": "done"}])
        topic_memory.add(self.state, TID, "回滚期间话题记的", now=later)
        meta = json.loads(project_memory.meta_path(self.state, PID).read_text())["entries"]
        self.assertFalse(set().union(*map(set, meta.values())) - set(project_memory.META_KEYS))
        # Upgrade again.
        memstore._cache.clear()
        self.assertGreater(migrate._memory_left(self.state), 0)
        self.assertEqual(self.run_step(later + 60)[1], [])
        glob = self.by_text(scope="global")
        self.assertEqual(glob["回滚期间新记的原则"].status, "active")
        self.assertEqual(glob["回滚期间记的做法"].kind, "rule")
        self.assertEqual(glob["开工前先查看当前状态再动手"].status, "active")  # Followed: not changed here.
        self.assertNotIn("开工前先查看当前状态", glob)
        self.assertEqual((glob["回复主人用中文"].title, glob["回复主人用中文"].code), ("中文回复", retitled.code))
        self.assertIn(("conflict", "回复主人一律用中文"), {(i["type"], i["text"]) for i in memstore.review_items(self.state)})
        self.assertEqual(glob["写文档只写实际有的数据"].status, archived.status)
        self.assertEqual(glob["只有新版才有的规矩"].code, new_global.code)
        mine = self.by_text(scope="project", owner_key=PID)
        self.assertEqual((mine["包 B 已合入"].title, mine["包 B 已合入"].repos, mine["包 B 已合入"].status),
                         ("包 B", ("covey",), "archived"))
        self.assertEqual(mine["包 B 已合入"].code, noted.code)
        self.assertEqual(mine["回滚期间负责人记的"].kind, "note")  # Not carried by any turn: a note.
        self.assertIn(f"project:{PID}:{added['id']}", {e.ref for e in memstore.entries(self.state, scope="project")})
        topic = self.by_text(scope="topic", owner_key=TID)
        self.assertEqual(topic["这个话题只读"].code, topic_entry.code)
        self.assertIn("回滚期间话题记的", topic)
        after = {e.ref: e.code for e in memstore.entries(self.state, statuses=None)}
        after.update({e.ref: e.code for e in topic.values()})
        self.assertEqual({ref: after.get(ref) for ref in codes}, codes)  # No code changed.

    def test_a_bad_principles_file_archives_nothing(self):
        self.run_step()
        principles.path(self.state).write_text("{not json")
        self.run_step(NOW + 60)
        self.assertEqual({e.status for e in memstore.entries(self.state, scope="global", statuses=None)
                          if e.aliases[0].startswith("principle:")}, {"active"})

    def test_start_reads_no_topic_and_runs_again_only_when_an_older_store_changed(self):
        for index in range(40):
            topic_memory.add(self.state, f"{index:064x}", "一条")
        with mock.patch.object(memstore, "_topic", side_effect=AssertionError("read a topic")), \
                mock.patch.object(memstore, "seed_topics", side_effect=AssertionError("walked the topics")):
            summary = migrate.migrate(self.state)
            self.assertEqual(summary[migrate.MEMORY_V2]["failed"], 0)
            self.assertEqual(migrate.migrate(self.state), {})  # Done and nothing changed: not run.
            principles.save(self.state, principles.OWNER, "后来又记的")
            self.assertEqual(migrate.pending(self.state), {migrate.MEMORY_V2: 1})
            self.assertEqual(list(migrate.migrate(self.state)), [migrate.MEMORY_V2])
        self.assertIn("后来又记的", self.by_text(scope="global"))

    def test_the_way_back_writes_an_older_releases_files_and_is_not_taken_twice(self):
        self.run_step()
        rule = memstore.add(self.state, kind="rule", scope="global", text="新版记的规矩", now=NOW + 10)
        memstore.add(self.state, kind="note", scope="global", text="新版记的资料", now=NOW + 10)
        decision = memstore.add_decision(self.state, scope="project", owner_key=PID, question="合入吗？", answer="合入",
                                         question_ref="fedcba9876543210-1", now=NOW + 10)
        out = memstore.export_legacy(self.state, now=NOW + 20)
        self.assertEqual(out, {"principles": 1, "knowledge": 1, "decisions": 1, "left": 0})
        self.assertIn("新版记的规矩", [item["text"] for item in principles.load(self.state)["owner"]])
        self.assertIn("新版记的资料", [row["text"] for row in knowledge.entries(self.state)])
        self.assertIn("合入吗？→合入", [row["text"] for row in project_memory.live(self.state, projects.read_record(self.state, PID))])
        count = len(memstore.entries(self.state, statuses=None))
        self.run_step(NOW + 60)
        self.assertEqual(len(memstore.entries(self.state, statuses=None)), count)
        self.assertEqual(memstore.find(self.state, rule.code).ref, rule.ref)
        self.assertEqual(len([e for e in memstore.decisions(self.state, "project", PID) if "合入" in e.text]), 1)
        self.assertEqual(memstore.export_legacy(self.state, now=NOW + 70)["decisions"], 0)  # Once.
        self.assertEqual(memstore.find(self.state, decision.ref).status, "active")

    def test_a_deployment_without_memory_gets_no_backup(self):
        empty = state_dir(self)
        self.assertEqual(migrate.memory_v2(empty), (0, []))
        self.assertFalse(Path(empty, "backups").exists())
        self.assertEqual(migrate._memory_left(empty), 0)


class ScriptTest(Case):
    def test_the_script_reports_applies_and_counts(self):
        import io
        from contextlib import redirect_stdout
        import importlib.util
        spec = importlib.util.spec_from_file_location("memory_migrate", Path(__file__).resolve().parents[1] / "scripts" /
                                                      "memory_migrate.py")
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        import time
        os.utime(Path(self.state, "jobs", "j0.json"), (time.time() - 30 * DAY,) * 2)  # The script runs at the real time.
        os.utime(Path(self.state, "jobs", "j1.json"), (time.time() - DAY,) * 2)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(script.main(["--state", str(self.state), "--dry-run", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["would_take"], 1)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(script.main(["--state", str(self.state), "--apply", "--json"]), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["entries"]["global"]["rule/active/owner"], 4)
        self.assertEqual(report["review_open"], 2)


if __name__ == "__main__":
    unittest.main()
