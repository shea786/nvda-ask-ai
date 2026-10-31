"""Feature 4: conversations saved as JSON, continued later, managed, imported."""

import datetime
import json
import os
import shutil
import tempfile
import time
import unittest

import helpers  # noqa: F401  (adds the add-on package to sys.path)

import conversations
from conversations import Conversation, ConversationStore


class TestConversation(unittest.TestCase):
	def test_titleFromFirstQuestion(self):
		conversation = Conversation()
		conversation.addMessage("user", "  How   do I make\nbread at home?  ")
		conversation.addMessage("assistant", "Mix flour...")
		conversation.addMessage("user", "Second question")
		self.assertEqual(conversation.title, "How do I make bread at home?")
		long = Conversation()
		long.addMessage("user", "word " * 40)
		self.assertLessEqual(len(long.title), conversations.TITLE_LENGTH + 1)
		self.assertTrue(long.title.endswith("…"))

	def test_renameAndReset(self):
		conversation = Conversation()
		conversation.addMessage("user", "Original question")
		conversation.rename("  My trip  ")
		self.assertEqual((conversation.title, conversation.titleIsCustom), ("My trip", True))
		conversation.addMessage("user", "Another")
		self.assertEqual(conversation.title, "My trip", "a custom title is kept")
		conversation.rename("")
		self.assertEqual((conversation.title, conversation.titleIsCustom), ("Original question", False))

	def test_historySkipsUnansweredQuestionsAndNotes(self):
		conversation = Conversation()
		for role, text in (
			("user", "q1"),
			("assistant", "a1"),
			("user", "failed q"),
			("error", "Network error"),
			("user", "q2"),
			("assistant", "a2"),
			("note", "Created: x.docx"),
		):
			conversation.addMessage(role, text)
		self.assertEqual(conversation.history, [("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2")])
		self.assertEqual(conversation.turnCount, 5)
		self.assertEqual(conversation.lastAnswer, "a2")
		self.assertEqual(conversation.questionFor(conversation.lastAnswerIndex), "q2")

	def test_removeLastMessage(self):
		conversation = Conversation()
		conversation.addMessage("user", "only question")
		self.assertFalse(conversation.removeLastMessage("user", "other"))
		self.assertTrue(conversation.removeLastMessage("user", "only question"))
		self.assertEqual((conversation.messages, conversation.title), ([], ""))

	def test_messageCap(self):
		conversation = Conversation()
		for i in range(conversations.MAX_MESSAGES + 10):
			conversation.addMessage("user", str(i))
		self.assertEqual(len(conversation.messages), conversations.MAX_MESSAGES)
		self.assertEqual(conversation.messages[-1]["text"], str(conversations.MAX_MESSAGES + 9))

	def test_unknownRoleRejected(self):
		with self.assertRaises(ValueError):
			Conversation().addMessage("system", "x")

	def test_roundTrip(self):
		conversation = Conversation(projectFolder="C:\\Projects\\Book", model="gpt-6.1-sol", reasoningEffort="xhigh")
		conversation.addMessage("user", "Caf\u00e9?")
		conversation.addMessage("assistant", "**Yes**")
		data = json.loads(json.dumps(conversation.toDict()))
		self.assertEqual(data["schemaVersion"], conversations.SCHEMA_VERSION)
		copy = Conversation.fromDict(data)
		self.assertEqual(copy.toDict(), conversation.toDict())

	def test_fromDictRejectsBadData(self):
		for bad in (
			[],
			{"id": "not-hex"},
			{"id": "a" * 32, "schemaVersion": conversations.SCHEMA_VERSION + 1},
			{"id": "a" * 32, "schemaVersion": "1"},
		):
			with self.assertRaises(ValueError, msg=repr(bad)):
				Conversation.fromDict(bad)
		tolerant = Conversation.fromDict(
			{"id": "b" * 32, "messages": [{"role": "user", "text": "ok"}, {"role": "hacker", "text": "x"}, "junk"], "title": 5},
		)
		self.assertEqual([m["text"] for m in tolerant.messages], ["ok"])
		self.assertEqual(tolerant.title, "")

	def test_friendlyTime(self):
		now = datetime.datetime(2026, 10, 31, 15, 0).timestamp()
		self.assertEqual(conversations.friendlyTime(datetime.datetime(2026, 10, 31, 9, 5).timestamp(), now), "Today 09:05")
		self.assertEqual(
			conversations.friendlyTime(datetime.datetime(2026, 10, 30, 23, 59).timestamp(), now, yesterdayLabel="Gestern"),
			"Gestern 23:59",
		)
		self.assertEqual(conversations.friendlyTime(datetime.datetime(2026, 1, 2, 8, 0).timestamp(), now), "2026-01-02 08:00")

	def test_defaultFolder(self):
		self.assertEqual(
			conversations.defaultConversationsDir({"LOCALAPPDATA": "/lad"}, isWindows=True),
			os.path.join("/lad", "askAI", "conversations"),
		)
		self.assertEqual(
			conversations.defaultConversationsDir({}, isWindows=False),
			os.path.join(tempfile.gettempdir(), "askAI-conversations"),
		)


class TestStore(unittest.TestCase):
	def setUp(self):
		self.folder = os.path.join(tempfile.mkdtemp(prefix="askgpt-store-"), "conversations")
		self.addCleanup(shutil.rmtree, os.path.dirname(self.folder), True)
		self.errors = []
		self.store = ConversationStore(self.folder, onError=self.errors.append)

	def make(self, title, updated, archived=False):
		conversation = Conversation(title=title, updated=updated, archived=archived)
		conversation.addMessage("user", title, when=updated)
		self.store.save(conversation)
		return conversation

	def test_saveIsAtomicAndLoads(self):
		conversation = self.make("first", 10)
		self.assertEqual(sorted(os.listdir(self.folder)), [conversation.id + ".json"], "no temporary files left")
		loaded = self.store.load(conversation.id)
		self.assertEqual(loaded.toDict(), conversation.toDict())
		with open(os.path.join(self.folder, conversation.id + ".json"), encoding="utf-8") as f:
			self.assertEqual(json.load(f)["schemaVersion"], 3)
		self.assertIsNone(self.store.load("f" * 32))
		self.assertIsNone(self.store.load("../../etc/passwd"))

	def test_failedSaveKeepsTheOldFile(self):
		conversation = self.make("keep me", 10)
		path = os.path.join(self.folder, conversation.id + ".json")
		with open(path, "rb") as f:
			before = f.read()
		original = os.replace

		def failingReplace(*args):
			raise OSError("disk full")

		conversation.addMessage("user", "new")
		conversations.os.replace = failingReplace
		try:
			with self.assertRaises(OSError):
				self.store.save(conversation)
		finally:
			conversations.os.replace = original
		with open(path, "rb") as f:
			self.assertEqual(f.read(), before)
		self.assertEqual(os.listdir(self.folder), [conversation.id + ".json"], "the temporary file was removed")

	def test_listNewestFirstAndArchived(self):
		old = self.make("old", 10)
		new = self.make("new", 30)
		archived = self.make("archived", 50, archived=True)
		self.assertEqual([c.title for c in self.store.list()], ["new", "old"])
		self.assertEqual([c.title for c in self.store.list(includeArchived=True)], ["archived", "new", "old"])
		self.assertEqual(self.store.latest().id, new.id, "latest skips archived conversations")
		self.assertTrue(self.store.delete(old.id))
		self.assertFalse(self.store.delete(old.id))
		self.assertEqual([c.title for c in self.store.list(includeArchived=True)], ["archived", "new"])
		self.assertEqual(archived.title, "archived")

	def test_corruptFilesAreSkippedAndReported(self):
		good = self.make("good", 10)
		with open(os.path.join(self.folder, "c" * 32 + ".json"), "w", encoding="utf-8") as f:
			f.write("{not json")
		with open(os.path.join(self.folder, "notes.json"), "w", encoding="utf-8") as f:
			f.write("{}")
		other = Conversation()
		with open(os.path.join(self.folder, "d" * 32 + ".json"), "w", encoding="utf-8") as f:
			json.dump(other.toDict(), f)  # Id does not match the file name.
		self.assertEqual([c.id for c in self.store.list()], [good.id])
		self.assertEqual(len(self.errors), 3, self.errors)

	def test_missingFolderListsNothing(self):
		self.assertEqual(ConversationStore(os.path.join(self.folder, "nope")).list(), [])
		self.assertIsNone(ConversationStore(os.path.join(self.folder, "nope")).latest())

	def test_import(self):
		source = Conversation(archived=True)
		source.addMessage("user", "imported question")
		exportPath = os.path.join(os.path.dirname(self.folder), "copy.json")
		with open(exportPath, "w", encoding="utf-8") as f:
			json.dump(source.toDict(), f)
		imported = self.store.importFile(exportPath)
		self.assertEqual(imported.id, source.id)
		self.assertFalse(imported.archived, "imported conversations show up in the list")
		# Importing the same file again keeps one copy; a different conversation with the same id gets a new id.
		self.assertEqual(self.store.importFile(exportPath).id, source.id)
		source.addMessage("assistant", "different")
		source.archived = False
		with open(exportPath, "w", encoding="utf-8") as f:
			json.dump(source.toDict(), f)
		second = self.store.importFile(exportPath)
		self.assertNotEqual(second.id, source.id)
		self.assertEqual(len(self.store.list()), 2)
		with open(exportPath, "w", encoding="utf-8") as f:
			f.write("[]")
		with self.assertRaises(ValueError):
			self.store.importFile(exportPath)

	def test_updatedOrderFollowsTouch(self):
		first = self.make("first", time.time() - 100)
		second = self.make("second", time.time() - 50)
		first.addMessage("user", "newer")
		self.store.save(first)
		self.assertEqual(self.store.latest().id, first.id)
		self.assertEqual(second.title, "second")


if __name__ == "__main__":
	unittest.main()
