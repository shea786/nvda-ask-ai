"""Tests for search.py: finding text in the Conversation box and filtering conversations."""

import unittest

import helpers  # noqa: F401  (adds the add-on package to sys.path)

import search
from conversations import Conversation

TEXT = "You: What is Paris?\n\nChatGPT: paris is the capital.\n\nYou: And PARIS in winter?\n\nChatGPT: Cold."


class TestFind(unittest.TestCase):
	def test_caseInsensitive(self):
		matches = search.findMatches(TEXT, "Paris")
		self.assertEqual(len(matches), 3)
		self.assertEqual([TEXT[m:m + 5].lower() for m in matches], ["paris"] * 3)
		self.assertEqual(search.findMatches(TEXT, "  paris "), matches, "the query is trimmed")
		self.assertEqual(search.findMatches(TEXT, "london"), [])
		self.assertEqual(search.findMatches(TEXT, ""), [])
		self.assertEqual(search.findMatches("", "x"), [])

	def test_nonOverlapping(self):
		self.assertEqual(search.findMatches("aaaa", "aa"), [0, 2])

	def test_offsetsStayAlignedWhenCasefoldChangesLength(self):
		text = "Straße und STRASSE"
		matches = search.findMatches(text, "straße")
		self.assertEqual(matches, [0])

	def test_lineAt(self):
		start = search.findMatches(TEXT, "winter")[0]
		self.assertEqual(search.lineAt(TEXT, start), "You: And PARIS in winter?")
		self.assertEqual(search.lineAt("only line", 3), "only line")

	def test_stepWraps(self):
		self.assertEqual(search.step(-1, 3, True), (0, False))
		self.assertEqual(search.step(-1, 3, False), (2, False))
		self.assertEqual(search.step(1, 3, True), (2, False))
		self.assertEqual(search.step(2, 3, True), (0, True))
		self.assertEqual(search.step(0, 3, False), (2, True))
		self.assertEqual(search.step(0, 0, True), (-1, False))

	def test_nearestMatch(self):
		self.assertEqual(search.nearestMatch([5, 10, 20], 0), 0)
		self.assertEqual(search.nearestMatch([5, 10, 20], 11), 2)
		self.assertEqual(search.nearestMatch([5, 10, 20], 21), 0, "wraps")
		self.assertEqual(search.nearestMatch([5, 10, 20], 10, forward=False), 0)
		self.assertEqual(search.nearestMatch([5, 10, 20], 3, forward=False), 2, "wraps backwards")
		self.assertEqual(search.nearestMatch([], 3), -1)

	def test_controlPosition(self):
		value = "ab\ncd\nef"
		# A control that counts line breaks once (GetLastPosition == len(value)).
		self.assertEqual(search.controlPosition(value, 6, len(value)), 6)
		# Windows multi-line edit: "\r\n" counts as two positions.
		self.assertEqual(search.controlPosition(value, 6, len(value) + 2), 8)
		self.assertEqual(search.controlPosition(value, 1, len(value) + 2), 1)
		self.assertEqual(search.controlPosition("no breaks", 3, 9), 3)


class TestFilterConversations(unittest.TestCase):
	def test_titleAndMessageText(self):
		trip = Conversation(title="Trip to Paris")
		recipe = Conversation()
		recipe.addMessage("user", "How do I bake bread?")
		recipe.addMessage("assistant", "Use **flour**, water and YEAST.")
		self.assertEqual(search.filterConversations([trip, recipe], "paris"), [trip])
		self.assertEqual(search.filterConversations([trip, recipe], "yeast"), [recipe])
		self.assertEqual(search.filterConversations([trip, recipe], ""), [trip, recipe])
		self.assertEqual(search.filterConversations([trip, recipe], "nothing"), [])


if __name__ == "__main__":
	unittest.main()
