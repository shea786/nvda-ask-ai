"""Tests for status.py: the "Report Ask AI status" sentence and the Done/Failed announcements."""

import time
import unittest

import helpers  # noqa: F401  (adds the add-on package to sys.path)

import status
from status import StatusSnapshot

NOW = time.mktime((2026, 10, 31, 15, 5, 0, 0, 0, -1))


class TestStatusText(unittest.TestCase):
	def test_answering(self):
		snapshot = StatusSnapshot(answering=("Claude", "Trip", NOW - 40), dictating=True, signingIn=True)
		self.assertEqual(status.statusText(snapshot, NOW), "Claude, working for 40 seconds. Conversation: Trip.")
		snapshot = StatusSnapshot(answering=("ChatGPT", "", NOW - 125))
		self.assertEqual(status.statusText(snapshot, NOW), "ChatGPT, working for 2 minutes 5 seconds. Conversation: Untitled conversation.")
		snapshot = StatusSnapshot(answering=("ChatGPT", "Trip to Paris.", NOW - 0.4))
		self.assertEqual(status.statusText(snapshot, NOW), "ChatGPT, just started. Conversation: Trip to Paris.", "no 0 seconds, no ..")
		self.assertEqual(status.formatElapsed(1), "1 second")

	def test_questionTitleNotDoublePunctuated(self):
		snapshot = StatusSnapshot(answering=("ChatGPT", "What is the capital of France?", NOW - 3))
		self.assertEqual(status.statusText(snapshot, NOW), "ChatGPT, working for 3 seconds. Conversation: What is the capital of France?")
		snapshot = StatusSnapshot(answering=("ChatGPT", "Help!", NOW))
		self.assertEqual(status.statusText(snapshot, NOW), "ChatGPT, just started. Conversation: Help!")
		snapshot = StatusSnapshot(lastAnswer=("Claude", NOW, "Why?"), attachedFiles=1)
		self.assertEqual(status.statusText(snapshot, NOW), "Idle. Last answer from Claude at 3:05 PM in Why? 1 file attached to the next question.")

	def test_priorities(self):
		self.assertEqual(status.statusText(StatusSnapshot(dictating=True, signingIn=True), NOW), "Dictating.")
		self.assertEqual(status.statusText(StatusSnapshot(signingIn=True, signedOut=True, service="Claude"), NOW), "Waiting for sign-in.")
		snapshot = StatusSnapshot(service="Claude", signedOut=True, lastAnswer=("ChatGPT", NOW, "T"))
		self.assertEqual(status.statusText(snapshot, NOW), "Not signed in to Claude.")

	def test_idle(self):
		snapshot = StatusSnapshot(service="ChatGPT", lastAnswer=("ChatGPT", NOW, "Trip to Paris"))
		self.assertEqual(status.statusText(snapshot, NOW), "Idle. Last answer from ChatGPT at 3:05 PM in Trip to Paris.")
		self.assertEqual(status.statusText(StatusSnapshot(), NOW), "Idle.")

	def test_clock(self):
		self.assertEqual(status.formatClock(NOW, NOW), "3:05 PM")
		midnight = time.mktime((2026, 10, 31, 0, 7, 0, 0, 0, -1))
		self.assertEqual(status.formatClock(midnight, NOW), "12:07 AM")
		noon = time.mktime((2026, 10, 31, 12, 0, 0, 0, 0, -1))
		self.assertEqual(status.formatClock(noon, NOW), "12:00 PM")
		self.assertEqual(status.formatClock(NOW - 86400, NOW), "2026-10-30 3:05 PM")

	def test_doneAndFailed(self):
		self.assertEqual(status.doneText("Trip to Paris", "Paris is lovely."), "Ask AI answer, Trip to Paris: Paris is lovely.")
		self.assertEqual(status.doneText("What is up?", "Sun."), "Ask AI answer, What is up?: Sun.")
		self.assertEqual(status.doneText("", ""), "Ask AI answer, Untitled conversation:")
		self.assertEqual(status.failedText("Trip", "Couldn't reach Claude."), "Failed: Trip. Couldn't reach Claude.")
		self.assertEqual(status.failedText("What is up?", "Net."), "Failed: What is up? Net.", "no doubled punctuation")
		self.assertEqual(status.failedText("Trip.", "Net."), "Failed: Trip. Net.")

	def test_attachedToNextQuestion(self):
		snapshot = StatusSnapshot(contextApp="Word", attachedFiles=2)
		self.assertEqual(status.statusText(snapshot, NOW), "Idle. Context from Word and 2 files attached to the next question.")
		self.assertEqual(status.statusText(StatusSnapshot(attachedFiles=1), NOW), "Idle. 1 file attached to the next question.")
		self.assertEqual(status.statusText(StatusSnapshot(contextApp="Edge"), NOW), "Idle. Context from Edge attached to the next question.")

	def test_localeClock(self):
		snapshot = StatusSnapshot(lastAnswer=("ChatGPT", NOW, "Trip"))
		self.assertEqual(status.statusText(snapshot, NOW, clock=lambda t: "15:05"), "Idle. Last answer from ChatGPT at 15:05 in Trip.")

	def test_translatedTemplates(self):
		templates = dict(status.DEFAULT_TEMPLATES, idle="Inactif.")
		self.assertEqual(status.statusText(StatusSnapshot(), NOW, templates), "Inactif.")


if __name__ == "__main__":
	unittest.main()
