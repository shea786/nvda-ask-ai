# -*- coding: UTF-8 -*-
# Ask GPT NVDA add-on: conversations saved as one JSON file each, so they survive NVDA restarts and
# can live in a synced folder (for example OneDrive) to continue on another PC.
# Pure standard library, no NVDA imports.

import datetime
import json
import os
import re
import tempfile
import time
import uuid

SCHEMA_VERSION = 1
FILE_SUFFIX = ".json"
TITLE_LENGTH = 60
# Older messages are dropped beyond this, to keep files small. GPT only ever sees the last few turns.
MAX_MESSAGES = 1000
# Roles stored in messages. Only "user" and "assistant" are sent back to GPT as context.
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_ERROR = "error"
ROLE_NOTE = "note"  # For example "Created: report.docx".
ROLES = (ROLE_USER, ROLE_ASSISTANT, ROLE_ERROR, ROLE_NOTE)
_ID = re.compile(r"^[0-9a-f]{32}$")


def defaultConversationsDir(env=None, isWindows=os.name == "nt"):
	"""%LOCALAPPDATA%\\askGPT\\conversations on Windows, else a folder in the temp directory."""
	env = os.environ if env is None else env
	localAppData = env.get("LOCALAPPDATA") if isWindows else None
	if localAppData:
		return os.path.join(localAppData, "askGPT", "conversations")
	return os.path.join(tempfile.gettempdir(), "askGPT-conversations")


def titleFromQuestion(question, length=TITLE_LENGTH):
	"""A short title from the first question: its first line, cut at a word boundary."""
	text = re.sub(r"\s+", " ", (question or "").strip())
	if len(text) <= length:
		return text
	cut = text[:length]
	space = cut.rfind(" ")
	return (cut[:space] if space > length // 2 else cut).rstrip(" ,.;:") + "…"


class Conversation:
	"""One conversation. Use ConversationStore to load and save it."""

	def __init__(
		self,
		id=None,
		title="",
		created=None,
		updated=None,
		archived=False,
		projectFolder="",
		model="",
		reasoningEffort="",
		messages=None,
		titleIsCustom=False,
	):
		now = time.time()
		self.id = id or uuid.uuid4().hex
		self.title = title or ""
		self.created = float(created) if created is not None else now
		self.updated = float(updated) if updated is not None else self.created
		self.archived = bool(archived)
		self.projectFolder = projectFolder or ""
		self.model = model or ""
		self.reasoningEffort = reasoningEffort or ""
		self.messages = list(messages or [])
		self.titleIsCustom = bool(titleIsCustom)

	# Messages

	def addMessage(self, role, text, when=None):
		if role not in ROLES:
			raise ValueError("Unknown role %r" % role)
		when = time.time() if when is None else when
		self.messages.append({"role": role, "text": text or "", "time": when})
		del self.messages[:-MAX_MESSAGES]
		if role == ROLE_USER and not self.titleIsCustom and not self.title:
			self.title = titleFromQuestion(text)
		self.touch(when)

	def removeLastMessage(self, role, text):
		"""Remove the newest message if it is exactly this one (for example a cancelled question)."""
		if self.messages and self.messages[-1].get("role") == role and self.messages[-1].get("text") == text:
			self.messages.pop()
			if not self.titleIsCustom and not any(m["role"] == ROLE_USER for m in self.messages):
				self.title = ""
			return True
		return False

	def touch(self, when=None):
		self.updated = time.time() if when is None else when

	def rename(self, title):
		"""Set a custom title; an empty title goes back to the automatic one (from the first question)."""
		title = (title or "").strip()
		if title:
			self.title = title
			self.titleIsCustom = True
		else:
			firstQuestion = next((m["text"] for m in self.messages if m.get("role") == ROLE_USER), "")
			self.title = titleFromQuestion(firstQuestion)
			self.titleIsCustom = False
		self.touch()

	@property
	def history(self):
		"""(role, text) pairs for the prompt: answered questions and their answers only.
		Questions that failed or were cancelled are left out, so GPT does not see them twice.
		"""
		result = []
		pendingQuestion = None
		for message in self.messages:
			role = message.get("role")
			if role == ROLE_USER:
				pendingQuestion = message["text"]
			elif role == ROLE_ASSISTANT:
				if pendingQuestion is not None:
					result.append((ROLE_USER, pendingQuestion))
					pendingQuestion = None
				result.append((ROLE_ASSISTANT, message["text"]))
		return result

	@property
	def turnCount(self):
		return sum(1 for m in self.messages if m.get("role") in (ROLE_USER, ROLE_ASSISTANT))

	def questionFor(self, answerIndex):
		"""The question asked before the message at answerIndex, or ""."""
		for message in reversed(self.messages[:answerIndex]):
			if message.get("role") == ROLE_USER:
				return message.get("text", "")
		return ""

	@property
	def lastAnswerIndex(self):
		for index in range(len(self.messages) - 1, -1, -1):
			if self.messages[index].get("role") == ROLE_ASSISTANT:
				return index
		return None

	@property
	def lastAnswer(self):
		for message in reversed(self.messages):
			if message.get("role") == ROLE_ASSISTANT:
				return message.get("text", "")
		return ""

	@property
	def isEmpty(self):
		return not self.messages

	# Serialisation

	def toDict(self):
		return {
			"schemaVersion": SCHEMA_VERSION,
			"id": self.id,
			"title": self.title,
			"titleIsCustom": self.titleIsCustom,
			"created": self.created,
			"updated": self.updated,
			"archived": self.archived,
			"projectFolder": self.projectFolder,
			"model": self.model,
			"reasoningEffort": self.reasoningEffort,
			"messages": self.messages,
		}

	@classmethod
	def fromDict(cls, data):
		"""Build from saved JSON. Raises ValueError for data that is not a usable conversation."""
		if not isinstance(data, dict):
			raise ValueError("not a JSON object")
		version = data.get("schemaVersion", 1)
		if not isinstance(version, int) or version > SCHEMA_VERSION:
			raise ValueError("unsupported schema version %r" % (version,))
		conversationId = data.get("id")
		if not isinstance(conversationId, str) or not _ID.match(conversationId):
			raise ValueError("missing or invalid id")
		messages = []
		for message in data.get("messages") or []:
			if (
				isinstance(message, dict)
				and message.get("role") in ROLES
				and isinstance(message.get("text"), str)
			):
				when = message.get("time")
				messages.append(
					{
						"role": message["role"],
						"text": message["text"],
						"time": float(when) if isinstance(when, (int, float)) else 0.0,
					},
				)

		def text(key):
			value = data.get(key)
			return value if isinstance(value, str) else ""

		def number(key):
			value = data.get(key)
			return float(value) if isinstance(value, (int, float)) else None

		return cls(
			id=conversationId,
			title=text("title"),
			created=number("created"),
			updated=number("updated"),
			archived=data.get("archived") is True,
			projectFolder=text("projectFolder"),
			model=text("model"),
			reasoningEffort=text("reasoningEffort"),
			messages=messages,
			titleIsCustom=data.get("titleIsCustom") is True,
		)


def friendlyTime(timestamp, now=None, todayLabel="Today", yesterdayLabel="Yesterday"):
	"""Local time as "Today 14:05", "Yesterday 09:30" or "2026-10-29 18:00"."""
	now = time.time() if now is None else now
	moment = datetime.datetime.fromtimestamp(timestamp)
	today = datetime.datetime.fromtimestamp(now).date()
	clock = moment.strftime("%H:%M")
	if moment.date() == today:
		return "%s %s" % (todayLabel, clock)
	if moment.date() == today - datetime.timedelta(days=1):
		return "%s %s" % (yesterdayLabel, clock)
	return moment.strftime("%Y-%m-%d %H:%M")


class ConversationStore:
	"""Conversations in a folder, one <id>.json file each. Corrupt files are skipped and reported."""

	def __init__(self, folder, onError=None):
		self.folder = folder
		self._onError = onError
		self.errors = []

	def _report(self, message):
		self.errors.append(message)
		if self._onError is not None:
			try:
				self._onError(message)
			except Exception:
				pass

	def pathFor(self, conversationId):
		if not _ID.match(conversationId or ""):
			raise ValueError("invalid conversation id %r" % (conversationId,))
		return os.path.join(self.folder, conversationId + FILE_SUFFIX)

	def save(self, conversation):
		"""Write atomically: a temporary file in the same folder, then os.replace."""
		os.makedirs(self.folder, exist_ok=True)
		path = self.pathFor(conversation.id)
		data = json.dumps(conversation.toDict(), ensure_ascii=False, indent=1).encode("utf-8")
		fd, tempPath = tempfile.mkstemp(prefix=".tmp-", suffix=FILE_SUFFIX, dir=self.folder)
		try:
			with os.fdopen(fd, "wb") as f:
				f.write(data)
				f.flush()
				os.fsync(f.fileno())
			os.replace(tempPath, path)
		except BaseException:
			try:
				os.remove(tempPath)
			except OSError:
				pass
			raise
		return path

	def _readPath(self, path):
		with open(path, "r", encoding="utf-8") as f:
			return Conversation.fromDict(json.load(f))

	def load(self, conversationId):
		"""The conversation, or None if missing or unreadable."""
		try:
			path = self.pathFor(conversationId)
		except ValueError:
			return None
		if not os.path.isfile(path):
			return None
		try:
			conversation = self._readPath(path)
		except (OSError, ValueError) as e:
			self._report("Skipping unreadable conversation %s: %s" % (path, e))
			return None
		if conversation.id != conversationId:
			self._report("Skipping %s: its id does not match the file name" % path)
			return None
		return conversation

	def list(self, includeArchived=False):
		"""All readable conversations, newest first."""
		result = []
		try:
			names = os.listdir(self.folder)
		except FileNotFoundError:
			return result
		except OSError as e:
			self._report("Cannot list %s: %s" % (self.folder, e))
			return result
		for name in names:
			if not name.endswith(FILE_SUFFIX) or name.startswith("."):
				continue
			conversation = self.load(name[: -len(FILE_SUFFIX)])
			if conversation is None:
				if _ID.match(name[: -len(FILE_SUFFIX)]) is None:
					self._report("Skipping %s: not a conversation file name" % name)
				continue
			if conversation.archived and not includeArchived:
				continue
			result.append(conversation)
		result.sort(key=lambda c: c.updated, reverse=True)
		return result

	def latest(self):
		"""The most recently updated non-archived conversation, or None."""
		conversations = self.list()
		return conversations[0] if conversations else None

	def delete(self, conversationId):
		try:
			os.remove(self.pathFor(conversationId))
			return True
		except FileNotFoundError:
			return False

	def importFile(self, path):
		"""Copy a conversation .json into the store. If its id is taken by a different conversation,
		it gets a new id. Returns the imported conversation. Raises ValueError/OSError for bad files.
		"""
		with open(path, "r", encoding="utf-8") as f:
			conversation = Conversation.fromDict(json.load(f))
		conversation.archived = False
		existing = self.load(conversation.id)
		if existing is not None and existing.toDict() != conversation.toDict():
			conversation.id = uuid.uuid4().hex
		self.save(conversation)
		return conversation
