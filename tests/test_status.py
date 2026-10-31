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
		self.assertEqual(status.statusText(snapshot, NOW), "Claude is answering in Trip, 40 seconds.")
		snapshot = StatusSnapshot(answering=("ChatGPT", "", NOW - 125))
		self.assertEqual(status.statusText(snapshot, NOW), "ChatGPT is answering in Untitled conversation, 2 minutes 5 seconds.")
		self.assertEqual(status.formatElapsed(1), "1 second")

	def test_priorities(self):
		self.assertEqual(status.statusText(StatusSnapshot(dictating=True, signingIn=True), NOW), "Dictating.")
		self.assertEqual(status.statusText(StatusSnapshot(signingIn=True, signedOut=True, service="Claude"), NOW), "Waiting for sign-in.")
		snapshot = StatusSnapshot(service="Claude", signedOut=True, lastAnswer=("ChatGPT", NOW, "T"))
		self.assertEqual(status.statusText(snapshot, NOW), "Not signed in to Claude")

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
		self.assertEqual(status.doneText("Trip", "Paris is lovely."), "Done: Trip. Paris is lovely.")
		self.assertEqual(status.doneText("", ""), "Done: Untitled conversation.")
		self.assertEqual(status.failedText("Trip", "Couldn't reach Claude."), "Failed: Trip. Couldn't reach Claude.")

	def test_translatedTemplates(self):
		templates = dict(status.DEFAULT_TEMPLATES, idle="Inactif.")
		self.assertEqual(status.statusText(StatusSnapshot(), NOW, templates), "Inactif.")


if __name__ == "__main__":
	unittest.main()
