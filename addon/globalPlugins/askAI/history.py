# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: recalling earlier questions with Up and Down in the Question box.
# The global history (the last 100 questions) lives in the data folder (%LOCALAPPDATA%\askAI\history.json),
# never in the NVDA configuration. Pure standard library, no NVDA imports.

import json
import os
import tempfile
import time

MAX_ENTRIES = 100
FILE_NAME = "history.json"
# Questions longer than this are not kept in the global history (they become attachments anyway).
MAX_QUESTION_CHARS = 32000

# Results of QuestionRecall.move().
NO_EARLIER = "noEarlier"
NO_LATER = "noLater"


def _replace(source, target, attempts=3, delay=0.1):
	"""os.replace, retried briefly on PermissionError (Windows: the file is open elsewhere)."""
	for attempt in range(attempts):
		try:
			os.replace(source, target)
			return
		except PermissionError:
			if attempt + 1 >= attempts:
				raise
			time.sleep(delay)


def shouldRecall(older, caretLine, lineCount, recalledUnedited, text):
	"""Up/Down recall rule: an empty box always; an untouched recalled entry only from its first line (Up)
	or last line (Down), or when it is a single line. Typed text never.
	"""
	if not text.strip():
		return True
	if not recalledUnedited:
		return False
	if lineCount <= 1:
		return True
	return caretLine <= 0 if older else caretLine >= lineCount - 1


def addEntry(entries, question, maxEntries=MAX_ENTRIES):
	"""entries (oldest first) with question added as the newest; an earlier copy is removed."""
	question = (question or "").strip()
	if not question or len(question) > MAX_QUESTION_CHARS:
		return list(entries)
	result = [entry for entry in entries if entry != question]
	result.append(question)
	return result[-maxEntries:]


class QuestionHistory:
	"""The global question history, saved as a JSON list (oldest first) with atomic writes."""

	def __init__(self, folder, maxEntries=MAX_ENTRIES):
		self.folder = folder
		self.maxEntries = maxEntries
		self.entries = []
		self.loaded = False

	@property
	def path(self):
		return os.path.join(self.folder, FILE_NAME)

	def load(self):
		"""Read the file; a missing or broken file gives an empty history. Returns the entries."""
		self.loaded = True
		try:
			with open(self.path, "r", encoding="utf-8") as f:
				data = json.load(f)
		except (OSError, ValueError):
			self.entries = []
			return self.entries
		questions = data.get("questions") if isinstance(data, dict) else data
		entries = []
		for item in questions if isinstance(questions, list) else []:
			if isinstance(item, str):
				entries = addEntry(entries, item, self.maxEntries)
		self.entries = entries
		return self.entries

	def add(self, question):
		if not self.loaded:
			self.load()
		self.entries = addEntry(self.entries, question, self.maxEntries)
		self.save()

	def save(self):
		os.makedirs(self.folder, exist_ok=True)
		data = json.dumps({"version": 1, "questions": self.entries}, ensure_ascii=False, indent=0).encode("utf-8")
		fd, tempPath = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=self.folder)
		try:
			with os.fdopen(fd, "wb") as f:
				f.write(data)
				f.flush()
				os.fsync(f.fileno())
			_replace(tempPath, self.path)
		except BaseException:
			try:
				os.remove(tempPath)
			except OSError:
				pass
			raise

	def clear(self):
		self.entries = []
		self.loaded = True
		self.save()


def recallList(conversationQuestions, globalQuestions):
	"""Questions to step through with Up, newest first: this conversation's, then the global history,
	without repeats.
	"""
	result = []
	seen = set()
	for source in (conversationQuestions, globalQuestions):
		for question in reversed(list(source)):
			question = (question or "").strip()
			if question and question not in seen:
				seen.add(question)
				result.append(question)
	return result


class QuestionRecall:
	"""Up/Down through earlier questions, like a shell history.
	index -1 is the text the user had typed; 0 is the newest earlier question.
	Recall only applies while the box is empty or still holds the recalled entry unchanged.
	"""

	def __init__(self):
		self.reset()

	def reset(self):
		self.entries = []
		self.index = -1
		self.typed = ""
		self.shown = None

	def canRecall(self, currentText):
		"""Up/Down recall when the box is empty or shows an untouched recalled entry."""
		if self.shown is not None and currentText == self.shown:
			return True
		return not currentText.strip()

	def move(self, currentText, entries, older=True):
		"""Returns (newText or None, result) where result is None, NO_EARLIER or NO_LATER.
		newText is the text to put in the box (None means leave it alone).
		"""
		if self.shown is None or currentText != self.shown:
			# A fresh recall: remember what the user had typed and take the current list.
			self.entries = list(entries)
			self.index = -1
			self.typed = currentText
		if older:
			if self.index + 1 >= len(self.entries):
				# Past the oldest entry: give the user's own text back (the next Up starts again at the newest).
				if self.index < 0:
					return None, NO_EARLIER
				self.index = -1
				self.shown = None
				return self.typed, NO_EARLIER
			self.index += 1
			self.shown = self.entries[self.index]
			return self.shown, None
		if self.index < 0:
			return None, NO_LATER
		self.index -= 1
		if self.index < 0:
			# Back past the newest entry: give the user's own text back.
			self.shown = None
			return self.typed, NO_LATER
		self.shown = self.entries[self.index]
		return self.shown, None
