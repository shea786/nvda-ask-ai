"""Tests for migration from the Ask GPT 1.x add-on (pure part)."""

import json
import os
import shutil
import tempfile
import types
import unittest

import helpers  # noqa: F401  (adds the add-on package to sys.path)

import migration

DEFAULTS = {
	"codexPath": "",
	"model": "",
	"reasoningEffort": "low",
	"instructions": "Default instructions.",
	"speakAnswers": True,
	"keepContext": True,
	"beepWhileWaiting": True,
	"allowProjectWrites": True,
	"conversationsDir": "",
	"dictationSilenceTimeout": 4,
	"timeout": 180,
}


class TestSettings(unittest.TestCase):
	def test_stringsFromNvdaIniAreConverted(self):
		# What configobj keeps for a section without a registered spec: plain strings.
		old = {
			"codexPath": "C:\\tools\\codex.cmd",
			"model": "gpt-6.1-sol",
			"reasoningEffort": "xhigh",
			"speakAnswers": "False",
			"beepWhileWaiting": "True",
			"timeout": "300",
			"dictationSilenceTimeout": "7",
			"conversationsDir": "D:\\OneDrive\\Ask GPT",
			"unknownKey": "x",
		}
		self.assertEqual(
			migration.migratedSettings(old, DEFAULTS),
			{
				"codexPath": "C:\\tools\\codex.cmd",
				"model": "gpt-6.1-sol",
				"reasoningEffort": "xhigh",
				"speakAnswers": False,
				"timeout": 300,
				"dictationSilenceTimeout": 7,
				"conversationsDir": "D:\\OneDrive\\Ask GPT",
			},
			"defaults and unknown keys are not copied",
		)

	def test_typedValuesAndBadValues(self):
		# When the old add-on is still running its spec has converted the values already.
		old = {"keepContext": False, "timeout": 45, "allowProjectWrites": "maybe", "dictationSilenceTimeout": "lots"}
		self.assertEqual(migration.migratedSettings(old, DEFAULTS), {"keepContext": False, "timeout": 45})
		self.assertEqual(migration.migratedSettings({}, DEFAULTS), {})
		self.assertEqual(migration.migratedSettings(None, DEFAULTS), {})


class TestConversations(unittest.TestCase):
	def setUp(self):
		self.root = tempfile.mkdtemp(prefix="askai-migration-")
		self.addCleanup(shutil.rmtree, self.root, True)
		self.old = os.path.join(self.root, "askGPT", "conversations")
		self.new = os.path.join(self.root, "askAI", "conversations")
		os.makedirs(self.old)
		for name in ("a" * 32, "b" * 32):
			with open(os.path.join(self.old, name + ".json"), "w", encoding="utf-8") as f:
				json.dump({"schemaVersion": 1, "id": name, "messages": []}, f)
		with open(os.path.join(self.old, ".tmp-x.json"), "w") as f:
			f.write("partial")

	def test_copiesWithoutMoving(self):
		self.assertEqual(migration.copyConversations(self.old, self.new), 2)
		self.assertEqual(sorted(os.listdir(self.new)), ["a" * 32 + ".json", "b" * 32 + ".json"])
		self.assertEqual(len([n for n in os.listdir(self.old) if n.endswith(".json")]), 3, "the originals stay")
		# Running again does nothing: the new folder has conversations now.
		self.assertEqual(migration.copyConversations(self.old, self.new), 0)

	def test_skipsWhenNewFolderHasConversations(self):
		os.makedirs(self.new)
		with open(os.path.join(self.new, "c" * 32 + ".json"), "w") as f:
			f.write("{}")
		self.assertEqual(migration.copyConversations(self.old, self.new), 0)
		self.assertEqual(os.listdir(self.new), ["c" * 32 + ".json"])

	def test_nothingToCopy(self):
		self.assertEqual(migration.copyConversations(os.path.join(self.root, "missing"), self.new), 0)
		self.assertFalse(os.path.exists(self.new))
		self.assertEqual(migration.copyConversations(self.old, self.old), 0)
		self.assertEqual(migration.copyConversations(None, self.new), 0)

	def test_oldFolder(self):
		self.assertEqual(
			migration.oldConversationsDir({"LOCALAPPDATA": "/lad"}),
			os.path.join("/lad", "askGPT", "conversations"),
		)
		self.assertIsNone(migration.oldConversationsDir({}))


class TestOldAddon(unittest.TestCase):
	def addon(self, name="askGPT", isEnabled=True, isPendingRemove=False):
		return types.SimpleNamespace(name=name, isEnabled=isEnabled, isPendingRemove=isPendingRemove)

	def test_findOldAddon(self):
		old = self.addon()
		self.assertIs(migration.findOldAddon([self.addon("askAI"), old]), old)
		self.assertIsNone(migration.findOldAddon([self.addon(isEnabled=False)]))
		self.assertIsNone(migration.findOldAddon([self.addon(isPendingRemove=True)]))
		self.assertIsNone(migration.findOldAddon([self.addon("other")]))
		self.assertIsNone(migration.findOldAddon(iter([])))
		legacy = types.SimpleNamespace(name="askGPT", isDisabled=False)
		self.assertIs(migration.findOldAddon([legacy]), legacy)


if __name__ == "__main__":
	unittest.main()
