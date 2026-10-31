"""Tests for history.py (question recall) and drafts in conversations.py."""

import json
import os
import shutil
import tempfile
import unittest

import helpers  # noqa: F401  (adds the add-on package to sys.path)

import history
from conversations import Conversation, ConversationStore, SCHEMA_VERSION


class TestHistoryFile(unittest.TestCase):
	def setUp(self):
		self.folder = tempfile.mkdtemp(prefix="askai-history-")
		self.addCleanup(shutil.rmtree, self.folder, True)

	def test_persistsDeduplicatedAndCapped(self):
		store = history.QuestionHistory(self.folder, maxEntries=3)
		for question in ("one", "two", "one", " three ", "", "four"):
			store.add(question)
		self.assertEqual(store.entries, ["one", "three", "four"])
		with open(os.path.join(self.folder, "history.json"), encoding="utf-8") as f:
			self.assertEqual(json.load(f)["questions"], ["one", "three", "four"])
		again = history.QuestionHistory(self.folder, maxEntries=3)
		self.assertEqual(again.load(), ["one", "three", "four"])
		self.assertFalse([name for name in os.listdir(self.folder) if name.startswith(".tmp")])

	def test_defaultLimitIs100AndLongQuestionsAreSkipped(self):
		entries = []
		for i in range(150):
			entries = history.addEntry(entries, "q%d" % i)
		self.assertEqual(len(entries), 100)
		self.assertEqual(entries[0], "q50")
		self.assertEqual(history.addEntry(["a"], "x" * 40000), ["a"])

	def test_brokenFileGivesEmptyHistory(self):
		with open(os.path.join(self.folder, "history.json"), "w") as f:
			f.write("{not json")
		self.assertEqual(history.QuestionHistory(self.folder).load(), [])
		with open(os.path.join(self.folder, "history.json"), "w") as f:
			json.dump({"questions": ["ok", 3, None, "ok"]}, f)
		self.assertEqual(history.QuestionHistory(self.folder).load(), ["ok"])


class TestRecall(unittest.TestCase):
	def test_recallList(self):
		self.assertEqual(
			history.recallList(["c1", "c2"], ["g1", "c1", "g2"]),
			["c2", "c1", "g2", "g1"],
			"this conversation first (newest first), then the global history without repeats",
		)

	def test_upAndDown(self):
		recall = history.QuestionRecall()
		entries = ["newest", "older", "oldest"]
		self.assertTrue(recall.canRecall(""))
		self.assertEqual(recall.move("", entries, older=True), ("newest", None))
		self.assertTrue(recall.canRecall("newest"))
		self.assertEqual(recall.move("newest", entries, older=True), ("older", None))
		self.assertEqual(recall.move("older", entries, older=True), ("oldest", None))
		# Past the oldest: back to what the user had typed.
		self.assertEqual(recall.move("oldest", entries, older=True), ("", history.NO_EARLIER))
		self.assertEqual(recall.move("", entries, older=True), ("newest", None), "starts again at the newest")
		self.assertEqual(recall.move("newest", entries, older=False), ("", history.NO_LATER))
		self.assertEqual(recall.move("", entries, older=False), (None, history.NO_LATER))

	def test_editedTextStopsRecall(self):
		recall = history.QuestionRecall()
		recall.move("", ["a question"], older=True)
		self.assertFalse(recall.canRecall("a question, edited"))
		self.assertFalse(recall.canRecall("typed text"))
		self.assertTrue(recall.canRecall("   "))

	def test_noEntries(self):
		recall = history.QuestionRecall()
		self.assertEqual(recall.move("", [], older=True), (None, history.NO_EARLIER))


class TestDrafts(unittest.TestCase):
	def setUp(self):
		self.folder = tempfile.mkdtemp(prefix="askai-drafts-")
		self.addCleanup(shutil.rmtree, self.folder, True)

	def test_draftAndMetadataRoundTrip(self):
		store = ConversationStore(self.folder)
		conversation = Conversation(draft="half a question")
		conversation.addMessage("user", "Q", metadata={"attachments": ["a.png", 3], "context": "12 characters from Word", "x": 1})
		conversation.addMessage("assistant", "A", metadata={"attachments": ["ignored-for-answers?"]})
		store.save(conversation)
		loaded = store.load(conversation.id)
		self.assertEqual(loaded.draft, "half a question")
		self.assertEqual(loaded.messages[0]["attachments"], ["a.png"])
		self.assertEqual(loaded.messages[0]["context"], "12 characters from Word")
		self.assertNotIn("x", loaded.messages[0])
		self.assertEqual(loaded.toDict()["schemaVersion"], SCHEMA_VERSION)
		self.assertEqual(SCHEMA_VERSION, 3)
		self.assertEqual(loaded.questions, ["Q"])

	def test_olderSchemasLoadWithoutDraft(self):
		for version in (1, 2):
			data = {"schemaVersion": version, "id": "a" * 32, "messages": [{"role": "user", "text": "q", "time": 1}]}
			loaded = Conversation.fromDict(data)
			self.assertEqual(loaded.draft, "")
			self.assertNotIn("attachments", loaded.messages[0])
		with self.assertRaises(ValueError):
			Conversation.fromDict({"schemaVersion": 4, "id": "a" * 32})


if __name__ == "__main__":
	unittest.main()
