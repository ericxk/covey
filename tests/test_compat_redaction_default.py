"""Two defaults of 敏感词脱敏 became one (T546, the owner 2026-11-19): the 脱敏 tab's 新话题默认开 (redaction/config.json
``default_on``, the launch card's) and the 新话题 page's 脱敏 row (main-thread.json ``topic_defaults.redaction``, the
butler's topics'). core/migrate.py redaction-default carries an on ``default_on`` into the row once; config.json keeps
it for an older release, and nothing reads it after."""
from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from covey.core import butler_settings, migrate
from covey.core.redaction import load_config, save_config


class RedactionDefaultTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = Path(folder.name)

    def topic_redaction(self):
        return butler_settings.load(self.state).topic_defaults.redaction

    def test_an_on_switch_turns_the_row_on_once_and_stays_in_the_config(self):
        save_config(self.state, {'default_on': True})
        self.assertEqual(migrate.pending(self.state)[migrate.REDACTION_DEFAULT], 1)
        summary = migrate.migrate(self.state)
        self.assertEqual(summary[migrate.REDACTION_DEFAULT], {'written': 1, 'failed': 0})
        self.assertTrue(self.topic_redaction())
        self.assertTrue(load_config(self.state)['default_on'])  # An older release still reads it.
        self.assertIn(migrate.REDACTION_DEFAULT, migrate.done(self.state))
        # Done once: the owner's later 关 on the row stays.
        settings = butler_settings.load(self.state)
        butler_settings.update(self.state, topic_defaults=dataclasses.replace(settings.topic_defaults, redaction=False))
        self.assertEqual(migrate.migrate(self.state), {})
        self.assertFalse(self.topic_redaction())

    def test_an_off_or_missing_switch_changes_nothing(self):
        for config in (None, {'default_on': False}):
            with self.subTest(config):
                if config is not None:
                    save_config(self.state, config)
                (self.state / migrate.MIGRATIONS).unlink(missing_ok=True)
                self.assertEqual(migrate.migrate(self.state)[migrate.REDACTION_DEFAULT], {'written': 0, 'failed': 0})
                self.assertFalse(self.topic_redaction())
                self.assertFalse((self.state / butler_settings.FILE).exists())

    def test_the_rest_of_the_settings_file_is_kept(self):
        butler_settings.update(self.state, model='opus', subagents='same',
                               topic_defaults=butler_settings.TopicDefaults(harness='claude', effort='high'))
        save_config(self.state, {'default_on': True})
        mode = json.loads((self.state / butler_settings.FILE).read_text())['mode']
        migrate.migrate(self.state)
        settings = butler_settings.load(self.state)
        self.assertEqual((settings.model, settings.subagents, settings.topic_defaults),
                         ('opus', 'same', butler_settings.TopicDefaults(harness='claude', effort='high',
                                                                               redaction=True)))
        self.assertEqual(json.loads((self.state / butler_settings.FILE).read_text())['mode'], mode)

    def test_a_file_without_a_mode_gets_none(self):
        """The mode is settle_mode's to write (a new deployment's 管家): the migration writes the one field."""
        (self.state / butler_settings.FILE).write_text(json.dumps({'model': 'opus'}))
        save_config(self.state, {'default_on': True})
        migrate.migrate(self.state)
        self.assertEqual(json.loads((self.state / butler_settings.FILE).read_text()),
                         {'model': 'opus', 'topic_defaults': {'redaction': True}})


if __name__ == '__main__':
    unittest.main()
