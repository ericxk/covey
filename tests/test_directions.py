"""方向标签 (core/directions.py, COVEY_DIRECTIONS=1): the file of the owner's directions, what decides the direction of a piece
of work (the owner's word, then the repos, then the butler's declaration, never the work directory alone), a new name
being a candidate until its second use, what the butler's topic_start says of it, and that with the switch off none of it
changes anything."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from support import enter_context
from support.records import state_dir

from covey.core import butler, butler_tool_specs, directions
from covey.core.state import write_json

STATE_ENV = butler.STATE_ENV


def entry(name, *, aliases=(), repos=(), status="active"):
    return {"name": name, "aliases": list(aliases), "repos": list(repos), "status": status}


class Case(unittest.TestCase):
    def setUp(self):
        self.state = state_dir(self)
        env = {name: value for name, value in os.environ.items() if not name.startswith(("COVEY_", "LARK_BRIDGE_"))}
        enter_context(self, mock.patch.dict(os.environ, {**env, directions.ENV: "1"}, clear=True))

    def write(self, *entries, bare=False):
        write_json(self.state / directions.FILE, list(entries) if bare else {"directions": list(entries)}, ensure_ascii=False)

    def saved(self):
        return json.loads((self.state / directions.FILE).read_text())["directions"]


class FileTest(Case):
    def test_a_missing_or_unreadable_file_has_no_entries_and_a_bare_list_reads_too(self):
        self.assertEqual(directions.load(self.state), [])
        (self.state / directions.FILE).write_text("{not json")
        self.assertEqual(directions.load(self.state), [])
        self.write(entry("数据平台"), bare=True)
        self.assertEqual([item["name"] for item in directions.load(self.state)], ["数据平台"])

    def test_an_entry_is_made_well_formed_and_one_without_a_name_is_left_out(self):
        self.write({"name": " 数据  平台 ", "aliases": ["数平", 3, ""], "repos": "x", "status": "bogus"}, {"aliases": ["x"]}, "text")
        [item] = directions.load(self.state)
        self.assertEqual((item["name"], item["aliases"], item["repos"], item["status"]), ("数据 平台", ["数平", "3"], [], "candidate"))

    def test_a_change_of_the_file_is_read_again(self):
        self.write(entry("甲"))
        self.assertEqual([item["name"] for item in directions.load(self.state)], ["甲"])
        self.write(entry("甲"), entry("乙"))
        self.assertEqual([item["name"] for item in directions.load(self.state)], ["甲", "乙"])

    def test_the_switch_is_only_one(self):
        self.assertTrue(directions.enabled())
        for value in ("", "0", "true"):
            with mock.patch.dict(os.environ, {directions.ENV: value}):
                self.assertFalse(directions.enabled())


class MatchTest(Case):
    def test_a_name_or_an_alias_finds_its_entry_whatever_the_case(self):
        entries = [entry("Data Platform", aliases=["数平"])]
        entries = [dict(item, uses=[]) for item in entries]
        self.assertEqual(directions.find(entries, "data platform")["name"], "Data Platform")
        self.assertEqual(directions.find(entries, " 数平 ")["name"], "Data Platform")
        self.assertIsNone(directions.find(entries, "别的"))
        self.assertIsNone(directions.find(entries, ""))

    def test_repos_match_a_path_at_or_under_it_and_a_name_by_whole_components(self):
        entries = [entry("甲", repos=["/work/orders"]), entry("乙", repos=["acme/billing"]), entry("丙", repos=["covey"])]
        hit = lambda workdir: (directions.by_repo(entries, workdir) or {}).get("name")  # noqa: E731
        self.assertEqual(hit("/work/orders"), "甲")
        self.assertEqual(hit("/work/orders/api/src"), "甲")
        self.assertIsNone(hit("/work/orders-old"))  # Not a path under it.
        self.assertEqual(hit("/home/me/acme/billing"), "乙")
        self.assertEqual(hit("/home/me/acme/billing/sub"), "乙")
        self.assertIsNone(hit("/home/me/acme/billing2"))
        self.assertEqual(hit("/srv/covey/"), "丙")
        self.assertIsNone(hit("/srv/covey-data"))
        for nothing in ("", "relative/path", None):
            self.assertIsNone(hit(nothing))

    def test_the_owners_word_beats_the_repo_and_the_repo_beats_the_butler(self):
        entries = [dict(entry("甲", repos=["/work/orders"]), uses=[]), dict(entry("乙", aliases=["B"]), uses=[])]
        self.assertEqual(directions.resolve(entries, said="B", workdir="/work/orders", declared="丙"), {"name": "乙", "source": "owner"})
        self.assertEqual(directions.resolve(entries, workdir="/work/orders/x", declared="乙"), {"name": "甲", "source": "repo"})
        self.assertEqual(directions.resolve(entries, workdir="/elsewhere", declared="b"), {"name": "乙", "source": "butler"})
        self.assertEqual(directions.resolve(entries, workdir="/elsewhere", declared="新方向"), {"name": "新方向", "source": "butler"})

    def test_the_work_directory_alone_makes_no_direction(self):
        self.write(entry("甲", repos=["/work/orders"]))
        self.assertIsNone(directions.claim(self.state, "k1", workdir="/work/misc/other"))
        self.assertEqual([item["name"] for item in self.saved()], ["甲"])  # No candidate was written for it.


class ClaimTest(Case):
    def test_a_new_name_is_a_candidate_and_becomes_active_with_its_second_use_under_another_key(self):
        found = directions.claim(self.state, "r1", declared="数据平台")
        self.assertEqual(found, {"name": "数据平台", "source": "butler"})
        self.assertEqual((self.saved()[0]["status"], directions.shown(self.state, found)), ("candidate", ""))
        directions.claim(self.state, "r1", declared="数据平台")  # The same request again counts nothing.
        self.assertEqual((self.saved()[0]["status"], directions.shown(self.state, found)), ("candidate", ""))
        directions.claim(self.state, "r2", declared="数据平台")
        self.assertEqual((self.saved()[0]["status"], directions.shown(self.state, found)), ("active", "数据平台"))
        self.assertEqual(len(self.saved()), 1)

    def test_an_alias_counts_for_the_entry_and_an_active_one_stays_as_it_is(self):
        self.write(entry("数据平台", aliases=["数平"], repos=["/work/dp"]))
        found = directions.claim(self.state, "r1", said="数平")
        self.assertEqual(found, {"name": "数据平台", "source": "owner"})
        self.assertEqual(self.saved(), [entry("数据平台", aliases=["数平"], repos=["/work/dp"])])  # Nothing rewritten.
        self.assertEqual(directions.shown(self.state, found), "数据平台")

    def test_the_repo_use_of_a_candidate_counts_too(self):
        self.write(entry("乙", repos=["/work/b"], status="candidate"))
        directions.claim(self.state, "r1", workdir="/work/b/src")
        directions.claim(self.state, "r2", workdir="/work/b")
        self.assertEqual(self.saved()[0]["status"], "active")

    def test_nothing_without_the_switch_a_direction_or_a_state_directory(self):
        self.assertIsNone(directions.claim(self.state, "r1"))
        self.assertIsNone(directions.claim("", "r1", declared="x"))
        with mock.patch.dict(os.environ, {directions.ENV: ""}):
            self.assertIsNone(directions.claim(self.state, "r1", declared="数据平台"))
            self.assertEqual(directions.shown(self.state, {"name": "数据平台"}), "")
        self.assertFalse((self.state / directions.FILE).exists())

    def test_shown_is_only_for_an_active_name_in_the_file(self):
        self.write(entry("甲"), entry("乙", status="candidate"))
        self.assertEqual([directions.shown(self.state, {"name": name}) for name in ("甲", "乙", "丙")], ["甲", "", ""])
        for bad in (None, "甲", {"name": ""}, {}):
            self.assertEqual(directions.shown(self.state, bad), "")

    def test_known_lists_the_active_names_first_with_their_aliases(self):
        self.write(entry("乙", status="candidate"), entry("甲", aliases=["A"]))
        self.assertEqual(directions.known(self.state), [("甲", ["A"]), ("乙", [])])


class ToolTest(Case):
    def listed(self, **extra):
        return {tool["name"]: tool for tool in butler_tool_specs.listed({STATE_ENV: str(self.state), **extra})}

    def test_off_topic_start_is_what_it_was(self):
        with mock.patch.dict(os.environ, {directions.ENV: ""}):
            base = self.listed()
        self.assertNotIn("direction", base["topic_start"]["input_schema"]["properties"])
        self.assertNotIn("direction_by_owner", base["topic_start"]["input_schema"]["properties"])

    def test_on_topic_start_takes_a_direction_and_names_the_known_ones(self):
        self.write(entry("数据平台", aliases=["数平"]), entry("新来的", status="candidate"))
        tool = self.listed(**{directions.ENV: "1"})["topic_start"]
        properties = tool["input_schema"]["properties"]
        self.assertEqual(properties["direction_by_owner"]["type"], "boolean")
        self.assertIn("数据平台（也叫 数平）；新来的", properties["direction"]["description"])
        self.assertNotIn("direction", tool["input_schema"]["required"])
        self.assertNotIn("direction", self.listed(**{directions.ENV: "1"})["topic_send"]["input_schema"]["properties"])

    def test_the_server_is_told_the_switch_and_a_lead_does_not_get_it(self):
        config = butler.mcp_config(Path("/r"), self.state, "k", "/py")
        self.assertEqual(config["mcpServers"][butler.MCP_SERVER]["env"]["COVEY_DIRECTIONS"], "1")
        with mock.patch.dict(os.environ, {directions.ENV: ""}):
            self.assertNotIn("COVEY_DIRECTIONS", butler.mcp_config(Path("/r"), self.state, "k", "/py")["mcpServers"][butler.MCP_SERVER]["env"])


if __name__ == "__main__":
    unittest.main()
