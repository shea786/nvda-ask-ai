# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: the Ask dialog, the settings panel, the request lifecycle and saved conversations.

import os
import subprocess
import threading

import wx

import addonHandler
import api
import config
import gui
import tones
import ui
from gui import guiHelper, nvdaControls
from gui.dpiScalingHelper import DpiScalingHelperMixinWithoutInit
from gui.settingsDialogs import NVDASettingsDialog, SettingsPanel
from logHandler import log

import tempfile
import time

from . import attachments, conversations, dictation, exporters, folderSnapshot, history, providers, search
from . import status as statusText
from .providers import base as providerBase
from .providers import modelCatalog
from .providers import ProviderError
from .textUtils import estimatedSpeechMs, formatTurn, stripMarkdown

addonHandler.initTranslation()

CONFIG_SECTION = "askAI"
# Each service's settings: its program path, default model and default reasoning effort.
# The Codex keys keep their Ask GPT 1.x names, so migrated settings need no renaming.
PROVIDER_CONFIG_KEYS = {
	"codex": {"path": "codexPath", "model": "model", "effort": "reasoningEffort"},
	"claude": {"path": "claudePath", "model": "claudeModel", "effort": "claudeReasoningEffort"},
}
# Set by the global plugin (see providers.base.defaultWorkDir).
workDir = None
# Keep at most this many entries in the Conversation box.
MAX_TRANSCRIPT_ENTRIES = 100
BEEP_INTERVAL_MS = 2000
DICTATION_POLL_MS = 400
# How long to wait for the user to finish signing in in the browser (Claude may need a pasted code).
LOGIN_TIMEOUT_SECONDS = 300
# Delay before announcing after moving focus, so the focus announcement does not swallow it.
ANNOUNCE_AFTER_FOCUS_MS = 100
# Settings panel layout (unscaled pixels). NVDA's settings area is about 566 pixels wide, and
# NVDA's own panels wrap descriptions at 544, so no row may need more than that.
PANEL_CONTENT_WIDTH = 544
STATUS_WIDTH = 360
PATH_MIN_WIDTH = 200
INSTRUCTIONS_MIN_WIDTH = 300
# Ask dialog layout (unscaled pixels); the dialog stays well under 800 pixels wide.
ASK_TEXT_WIDTH = 560
# Read-only multi-line, so that Tab reaches it (a single-line read-only box is skipped by Tab in wx).
STATUS_STYLE = wx.TE_READONLY | wx.TE_MULTILINE | wx.TE_NO_VSCROLL


def _once(func):
	"""Wrap func so that it runs at most once, whichever trigger fires first."""
	done = []

	def wrapper(*args):
		if not done:
			done.append(True)
			func(*args)

	return wrapper


def effortLabel(effort):
	labels = {
		# Translators: Reasoning effort level.
		"minimal": _("Minimal"),
		# Translators: Reasoning effort level.
		"low": _("Low (fastest)"),
		# Translators: Reasoning effort level.
		"medium": _("Medium"),
		# Translators: Reasoning effort level.
		"high": _("High"),
		# Translators: Reasoning effort level.
		"xhigh": _("Extra high"),
		# Translators: Reasoning effort level.
		"max": _("Maximum"),
		# Translators: Reasoning effort level.
		"ultra": _("Ultra (slowest)"),
	}
	return labels.get(effort, effort.capitalize())


def defaultModelLabel():
	# Translators: The model choice that lets the service pick the default model of the user's plan.
	return _("Default (your plan's default)")


def serviceName(providerId):
	"""The service as users know it: "ChatGPT" or "Claude" (product names, not translated)."""
	return providers.shortName(providerId)


def programName(providerId):
	"""The program: "Codex" or "Claude Code"."""
	return providers.get(providerId).programName


class PendingRequest:
	"""A question being answered. Owned by the module, so it survives closing the dialog,
	and tied to the conversation it was asked in.
	"""

	def __init__(
		self,
		client,
		question,
		conversationId,
		projectFolder="",
		providerId=providers.DEFAULT_PROVIDER,
		plan=None,
		title="",
	):
		self.client = client
		self.question = question
		self.conversationId = conversationId
		self.projectFolder = projectFolder
		self.providerId = providerId
		# attachments.RequestPlan with the request's private folder, or None.
		self.plan = plan
		self.title = title
		self.startedAt = time.time()
		self.cancelEvent = threading.Event()


class AppState:
	"""The current conversation, the saved conversations and the running request."""

	def __init__(self):
		self.current = None
		self.pending = None
		self._store = None
		self._storeFolder = None
		self._models = {}
		# Attached to the next question only (see "Ask about this" and "Attach files").
		self.pendingContext = None
		self.includeScreenshot = False
		self.pendingAttachments = attachments.PendingAttachments()
		# The last search in the Conversation box, offered again by Ctrl+F.
		self.lastSearch = ""
		self._history = None
		# (providerId, time, conversation title) of the newest answer, for the status report.
		self.lastAnswerInfo = None
		# Last known sign-in state per service (from status checks and request results); no process is run.
		self.loginStates = {}

	@property
	def history(self):
		"""The global question history (%LOCALAPPDATA%\\askAI\\history.json), loaded on first use."""
		if self._history is None:
			folder = providerBase.dataDir() or os.path.join(tempfile.gettempdir(), "askAI-data")
			self._history = history.QuestionHistory(folder)
			try:
				self._history.load()
			except Exception:
				log.debugWarning("Ask AI: could not read the question history", exc_info=True)
		return self._history

	def clearPendingInput(self):
		"""Forget the context, screenshot and files attached to the next question."""
		self.pendingContext = None
		self.includeScreenshot = False
		self.pendingAttachments = attachments.PendingAttachments()

	# Conversations

	def conversationsFolder(self):
		configured = providerBase.expandUserPath(getConf()["conversationsDir"])
		return configured or conversations.defaultConversationsDir()

	@property
	def store(self):
		folder = self.conversationsFolder()
		if self._store is None or self._storeFolder != folder:
			self._store = conversations.ConversationStore(
				folder,
				onError=lambda message: log.debugWarning("Ask AI: %s" % message),
			)
			self._storeFolder = folder
		return self._store

	def newConversation(self, providerId=None):
		providerId = providerId or defaultProvider()
		_path, model, effort = providerSettings(providerId)
		return conversations.Conversation(model=model, reasoningEffort=effort, provider=providerId)

	def ensureCurrent(self):
		"""The conversation to show: this session's, else the newest saved one, else a new one."""
		if self.current is None:
			try:
				self.current = self.store.latest()
			except Exception:
				log.error("Ask AI: could not read saved conversations", exc_info=True)
				self.current = None
			if self.current is None:
				self.current = self.newConversation()
		return self.current

	def save(self, conversation=None):
		"""Save a conversation that has content. Errors are logged and announced, never raised."""
		conversation = conversation or self.current
		if conversation is None or (conversation.isEmpty and not conversation.projectFolder and not conversation.draft):
			return False
		try:
			self.store.save(conversation)
			return True
		except Exception:
			log.error("Ask AI: could not save the conversation", exc_info=True)
			# Translators: Reported when a conversation could not be written to disk.
			ui.message(_("Could not save the conversation. Check the conversations folder in the Ask AI settings."))
			return False

	def conversationById(self, conversationId):
		if self.current is not None and self.current.id == conversationId:
			return self.current
		return self.store.load(conversationId)

	# Answers (raw text is kept; speech and the Conversation box use the stripped text)

	@property
	def lastAnswer(self):
		return self.current.lastAnswer if self.current is not None else ""

	@property
	def lastAnswerSpoken(self):
		raw = self.lastAnswer
		return stripMarkdown(raw) or raw

	# Models

	def modelsFor(self, providerId):
		if providerId not in self._models:
			self.reloadModels(providerId)
		return self._models[providerId]

	@property
	def models(self):
		"""The Codex models (kept for older callers)."""
		return self.modelsFor("codex")

	def reloadModels(self, providerId=None):
		"""Re-read the model lists (all services, or one). Returns the list for providerId (or Codex)."""
		for pid in [providerId] if providerId else providers.ids():
			try:
				self._models[pid] = providers.create(pid, workDir=workDir).models()
			except Exception:
				log.debugWarning("Ask AI: could not read the %s model list" % pid, exc_info=True)
				self._models[pid] = []
		return self._models[providerId or "codex"]


state = AppState()
# The client running a sign-in. Only read and written on the main thread.
_loginClient = None
# The open settings panel, if any, so background results can refresh it.
_activePanel = None
# The running dictation, so shutdown can always restore speech.
_activeDictation = None


def getConf():
	return config.conf[CONFIG_SECTION]


def defaultProvider():
	providerId = getConf()["provider"]
	return providerId if providers.isKnown(providerId) else providers.DEFAULT_PROVIDER


def providerSettings(providerId):
	"""(program path, default model, default reasoning effort) from the settings."""
	conf = getConf()
	keys = PROVIDER_CONFIG_KEYS.get(providerId, PROVIDER_CONFIG_KEYS[providers.DEFAULT_PROVIDER])
	return conf[keys["path"]], conf[keys["model"]], conf[keys["effort"]]


def makeClient(executablePath=None, conversation=None, providerId=None):
	"""A provider instance for the conversation's service (or providerId, or the default service)."""
	if conversation is not None:
		providerId = conversation.provider if providers.isKnown(conversation.provider) else providers.DEFAULT_PROVIDER
	providerId = providerId or defaultProvider()
	providerClass = providers.get(providerId)
	path, model, effort = providerSettings(providerId)
	projectFolder = ""
	if conversation is not None:
		model = conversation.model
		effort = modelCatalog.pickEffort(
			state.modelsFor(providerId),
			model,
			conversation.reasoningEffort or effort,
			fallback=providerClass.defaultEfforts,
		)
		projectFolder = conversation.projectFolder
	conf = getConf()
	return providerClass(
		executablePath=path if executablePath is None else executablePath,
		workDir=workDir,
		model=model,
		reasoningEffort=effort,
		timeout=conf["timeout"],
		projectFolder=projectFolder,
		allowProjectWrites=conf["allowProjectWrites"],
	)


def transcriptEntries(conversation):
	"""(text, isAnswer) for the Conversation box: the newest MAX_TRANSCRIPT_ENTRIES messages.
	Answers are labelled with the service that wrote them, e.g. "ChatGPT:" or "Claude:".
	"""
	entries = []
	for message in conversation.messages[-MAX_TRANSCRIPT_ENTRIES:] if conversation else []:
		role, text = message.get("role"), message.get("text", "")
		if role == conversations.ROLE_USER:
			# Translators: Speaker name for the user's lines in the conversation.
			entries.append((formatTurn(_("You"), text), False))
		elif role == conversations.ROLE_ASSISTANT:
			speaker = serviceName(message.get("provider") or conversation.provider)
			entries.append((formatTurn(speaker, stripMarkdown(text) or text), True))
		elif role == conversations.ROLE_ERROR:
			# Translators: Prefix for error lines in the conversation.
			entries.append((formatTurn(_("Error"), text), False))
		else:
			entries.append((text, False))
	return entries


def fileChangesText(created, changed):
	"""One line such as "Created: a.docx, b.pdf. Changed: x.txt", or "" when nothing changed."""
	parts = []
	for names, template in (
		# Translators: Lists files the AI created in the project folder. {files} is a comma-separated list.
		(created, _("Created: {files}")),
		# Translators: Lists files the AI changed in the project folder. {files} is a comma-separated list.
		(changed, _("Changed: {files}")),
	):
		if names:
			shown, more = folderSnapshot.summarize(names)
			files = ", ".join(shown)
			if more:
				# Translators: Ends a shortened file list, e.g. "a, b, c and 4 more".
				files = _("{files} and {count} more").format(files=files, count=more)
			parts.append(template.format(files=files))
	return ". ".join(parts)


def contextSummaryTemplate():
	# Translators: Summary of attached screen text, e.g. "1,240 characters from Microsoft Word, Report.docx".
	return _("{count} characters from {where}")


def attachmentLabel(item):
	"""List entry for an attachment, e.g. "notes.txt, text, 12 KB"."""
	kinds = {
		# Translators: Kind of attached file: sent as a picture.
		attachments.KIND_IMAGE: _("image"),
		# Translators: Kind of attached file: put into the question as text.
		attachments.KIND_TEXT: _("text"),
		# Translators: Kind of attached file: copied for the AI to open.
		attachments.KIND_FILE: _("file"),
	}
	size = max(1, (item.size + 1023) // 1024)
	# Translators: An attachment in the list. {name} file name, {kind} e.g. "image", {size} kilobytes.
	return _("{name}, {kind}, {size} KB").format(name=item.name, kind=kinds.get(item.kind, item.kind), size=size)


def attachmentProblem(error):
	if error.reason == attachments.REASON_TOO_LARGE:
		# Translators: Error when a file is too big to attach. {name} is the file name.
		return _("{name} is larger than 25 MB and cannot be attached.").format(name=error.name)
	if error.reason == attachments.REASON_DUPLICATE:
		# Translators: Error when the same file is attached twice. {name} is the file name.
		return _("{name} is already attached.").format(name=error.name)
	# Translators: Error when a file cannot be read. {name} is the file name.
	return _("{name} could not be read.").format(name=error.name)


def resetTimeText(error):
	"""When a usage limit resets, in words, or ""."""
	epoch = getattr(error, "resetEpoch", None)
	if epoch:
		try:
			return providers.claude.formatResetTime(epoch)
		except (OverflowError, OSError, ValueError):
			return ""
	return getattr(error, "resetText", "") or ""


def friendlyError(error):
	"""Translated, plain-language message for a ProviderError. Never includes technical detail."""
	kind = getattr(error, "kind", providerBase.KIND_OTHER)
	reason = getattr(error, "reason", "")
	providerId = getattr(error, "provider", "") or defaultProvider()
	service = serviceName(providerId)
	program = programName(providerId)
	if kind == providerBase.KIND_NOT_INSTALLED:
		if reason == providerBase.REASON_CUSTOM_PATH:
			# Translators: Error when the configured program path is wrong. {program} is e.g. "Codex", {path} the path.
			return _(
				"The {program} executable path set in the Ask AI settings does not exist: {path}. "
				"Correct it, or clear it to find {program} automatically."
			).format(program=program, path=error.detail)
		if reason == providerBase.REASON_NODE_MISSING:
			# Translators: Error when Node.js, needed by an npm installation, is missing. {program} is e.g. "Codex".
			return _(
				"Node.js was not found. {program} installed with npm needs it: install Node.js from nodejs.org "
				"or with the command winget install OpenJS.NodeJS.LTS, then restart NVDA."
			).format(program=program)
		if reason == providerBase.REASON_INCOMPLETE_INSTALL:
			# Translators: Error when an installation is broken. {program} is e.g. "Codex", {package} an npm package.
			return _(
				'The {program} installation is incomplete. Run "npm install -g {package}" again '
				"in a command prompt, then restart NVDA."
			).format(program=program, package=providers.get(providerId).npmPackage)
		if providerId == "claude":
			# Translators: Error when the Claude Code program cannot be found. Keep the commands unchanged.
			return _(
				'The Claude Code program was not found. In PowerShell, run "irm https://claude.ai/install.ps1 | iex", '
				'or in a command prompt run "npm install -g @anthropic-ai/claude-code", then restart NVDA.'
			)
		# Translators: Error when the Codex program cannot be found. Keep the command and address unchanged.
		return _(
			"The OpenAI Codex program was not found. Install Node.js from nodejs.org, then run "
			'"npm install -g @openai/codex" in a command prompt, then restart NVDA.'
		)
	if kind == providerBase.KIND_NOT_LOGGED_IN:
		# Translators: Error when the service is not signed in. {service} is "ChatGPT" or "Claude", {program} e.g. "Codex".
		return _(
			"{program} is not signed in to your {service} account. Sign in to {service} in NVDA Settings > Ask AI: "
			"open the NVDA menu, Preferences, Settings, Ask AI, choose {service} and press Sign in to {service}."
		).format(program=program, service=service)
	if kind == providerBase.KIND_USAGE_LIMIT:
		epoch = getattr(error, "resetEpoch", None)
		resets = resetTimeText(error)
		if resets and epoch:
			# Translators: Error when the plan's usage limit is reached. {time} is the local reset time, e.g. "15:00".
			return _("You have reached the usage limit of your {service} plan. It resets at {time}.").format(
				service=service,
				time=resets,
			)
		if resets:
			# Translators: Error when the plan's usage limit is reached. {time} is the reset time as the service
			# wrote it, e.g. "3pm (Europe/London)".
			return _("You have reached the usage limit of your {service} plan. It resets {time}.").format(
				service=service,
				time=resets,
			)
		# Translators: Error when the plan's usage limit is reached. {service} is "ChatGPT" or "Claude".
		return _("You have reached the usage limit of your {service} plan. Please try again later.").format(service=service)
	if kind == providerBase.KIND_NETWORK:
		# Translators: Error when the service cannot be reached. {service} is "ChatGPT" or "Claude".
		return _("Couldn't reach {service}. Check your internet connection and try again.").format(service=service)
	if kind == providerBase.KIND_TIMEOUT:
		# Translators: Error when no answer came in time.
		return _("No answer arrived in time. Try again, or increase the timeout in the Ask AI settings.")
	if kind == providerBase.KIND_CANCELLED:
		# Translators: Reported when the user cancels a question.
		return _("Request cancelled.")
	if kind == providerBase.KIND_CONFIG:
		if reason == providerBase.REASON_PROJECT_MISSING:
			# Translators: Error when the project folder was moved or deleted. {path} is the folder.
			return _("The project folder no longer exists: {path}. Choose another folder or clear it.").format(
				path=error.detail,
			)
		if reason == providerBase.REASON_OUTDATED:
			# Translators: Error when the installed program is too old. {program} is e.g. "Claude Code".
			return _(
				"This {program} version is too old for Ask AI. Run \"claude update\" in a command prompt, then try again."
			).format(program=program)
		# Translators: Error about an invalid setting. {message} explains it.
		return _("A setting is not valid: {message}").format(message=error.message)
	if reason == providerBase.REASON_EMPTY_ANSWER:
		# Translators: Error when the AI returned nothing. {service} is "ChatGPT" or "Claude".
		return _("{service} returned an empty answer. Please try asking again.").format(service=service)
	if reason == providerBase.REASON_BUSY:
		# Translators: Error when the service is overloaded (not the user's limit). {service} is "ChatGPT" or "Claude".
		return _("{service} is busy right now. Please try again in a moment.").format(service=service)
	# For other failures the client's message is already plain language without technical detail.
	# Translators: Generic error. {service} is "ChatGPT" or "Claude".
	return getattr(error, "message", "") or _("Something went wrong while asking {service}.").format(service=service)


def transcriptErrorText(error):
	"""Friendly message, then the technical detail after a label, for the Conversation box only."""
	text = friendlyError(error)
	detail = getattr(error, "detail", "")
	if detail and getattr(error, "reason", "") not in (providerBase.REASON_CUSTOM_PATH, providerBase.REASON_PROJECT_MISSING):
		# Translators: Introduces technical error details in the conversation.
		text += " " + _("Technical details: {detail}").format(detail=detail)
	return text


def showError(message, parent=None):
	# Translators: Title of error dialogs.
	title = _("Ask AI error")
	try:
		from gui.message import MessageDialog  # NVDA 2025.1 and later.
	except ImportError:
		gui.messageBox(message, title, wx.OK | wx.ICON_ERROR, parent)
		return
	MessageDialog.alert(message, title, parent)


def askYesNo(message, title, parent=None):
	"""Yes/No question; Escape answers No. Call on the main thread."""
	try:
		from gui.message import DefaultButtonSet, MessageDialog, ReturnCode  # NVDA 2025.1 and later.
	except ImportError:
		return gui.messageBox(message, title, wx.YES_NO | wx.ICON_QUESTION, parent) == wx.YES
	# MessageDialog.ask() would add a Cancel button; build the dialog with Yes and No only.
	dialog = MessageDialog(parent, message, title, buttons=DefaultButtonSet.YES_NO)
	dialog.SetEscapeId(ReturnCode.NO)
	return dialog.ShowModal() == ReturnCode.YES


def speakThen(message, callback):
	"""Speak message, then run callback once speech has finished (or after an estimated delay,
	for example when speech is off). Used before actions that steal focus or listen to the microphone.
	"""
	callback = _once(callback)
	try:
		import speech
		from speech.commands import CallbackCommand

		speech.speak([message, CallbackCommand(lambda: wx.CallAfter(callback), name="askAI.afterSpeech")])
		try:
			import braille

			braille.handler.message(message)
		except Exception:
			pass
	except Exception:
		log.debugWarning("Ask AI: speech callback unavailable", exc_info=True)
		ui.message(message)
	wx.CallLater(estimatedSpeechMs(message), callback)


def documentsFolder():
	"""Documents\\Ask AI, using the real (possibly redirected) Documents folder."""
	try:
		documents = wx.StandardPaths.Get().GetDocumentsDir()
	except Exception:
		documents = ""
	if not documents:
		documents = os.path.join(os.path.expanduser("~"), "Documents")
	return exporters.defaultSaveFolder(documents)


def formatChoices():
	"""(key, label) pairs for the "save as" format chooser."""
	labels = {
		# Translators: A file format in the save dialog.
		exporters.FORMAT_DOCX: _("Word document (.docx)"),
		# Translators: A file format in the save dialog.
		exporters.FORMAT_PDF: _("PDF document (.pdf)"),
		# Translators: A file format in the save dialog.
		exporters.FORMAT_TXT: _("Plain text (.txt)"),
		# Translators: A file format in the save dialog.
		exporters.FORMAT_HTML: _("Web page (.html)"),
		# Translators: A file format in the save dialog.
		exporters.FORMAT_MD: _("Markdown (.md)"),
	}
	return [(key, labels[key]) for key, _extension in exporters.FORMATS]


def chooseFormatAndName(parent, suggestedName, title):
	"""Ask for a format, then a file name. Returns (formatKey, baseName) or None if cancelled."""
	choices = formatChoices()
	with wx.SingleChoiceDialog(
		parent,
		# Translators: Prompt in the dialog that asks which file format to save.
		_("Choose a file format:"),
		title,
		[label for _key, label in choices],
	) as formatDialog:
		if formatDialog.ShowModal() != wx.ID_OK:
			return None
		formatKey = choices[formatDialog.GetSelection()][0]
	with wx.TextEntryDialog(
		parent,
		# Translators: Prompt for the file name when saving.
		_("File name (without extension):"),
		title,
		suggestedName,
	) as nameDialog:
		if nameDialog.ShowModal() != wx.ID_OK:
			return None
		baseName = exporters.sanitizeFileName(nameDialog.GetValue(), fallback=suggestedName or exporters.DEFAULT_BASE_NAME)
	return formatKey, baseName


def saveDocument(parent, folder, formatKey, baseName, docTitle, blocks, markdownBlocks=None):
	"""Write the file and announce it. Returns the path, or None after reporting an error."""
	try:
		path = exporters.export(folder, baseName, formatKey, docTitle, blocks, markdownBlocks)
	except Exception as e:
		log.error("Ask AI: saving failed", exc_info=True)
		# Translators: Error when a file could not be saved. {error} is the reason.
		showError(_("The file could not be saved: {error}").format(error=e), parent)
		return None
	# Translators: Announced after saving a file. {name} is the file name, {folder} the folder.
	ui.message(_("Saved {name} in {folder}").format(name=os.path.basename(path), folder=folder))
	return path


# Request lifecycle (main thread unless noted)


def buildRequestPrompt(question, conversation, attachmentSection=""):
	"""The prompt for a question: instructions (with the service's project folder rules), earlier
	conversation, attached screen text and files (fenced, as data), then the question.
	"""
	conf = getConf()
	history = conversation.history if conf["keepContext"] else []
	projectInstructions = ""
	if conversation.projectFolder:
		projectInstructions = providers.get(conversation.provider).projectInstructions(conf["allowProjectWrites"])
	return providerBase.buildPrompt(
		question,
		history,
		conf["instructions"],
		projectInstructions=projectInstructions,
		attachmentSection=attachmentSection,
	)


def startRequest(question, conversation):
	"""Send question with whatever is attached (screen context, screenshot, files). Main thread.
	A question over attachments.LONG_QUESTION_CHARS is sent as an attached file, question.txt.
	The attachments apply to this question only and are cleared afterwards.
	"""
	providerId = conversation.provider if providers.isKnown(conversation.provider) else providers.DEFAULT_PROVIDER
	items = list(state.pendingAttachments)
	longAttachment, sentQuestion = attachments.longQuestion(question)
	if longAttachment is not None:
		items.append(longAttachment)
	context = state.pendingContext
	screenshot = context.screenshot if context is not None and state.includeScreenshot else None
	plan = None
	section = ""
	if items or context is not None:
		directory = attachments.RequestFiles.create(workDir or providerBase.defaultWorkDir())
		plan = attachments.RequestPlan(directory, items, context, screenshot)
		section = plan.promptSection(providers.get(providerId).imagesAttachedDirectly)
	prompt = buildRequestPrompt(sentQuestion, conversation, section)
	shownQuestion = question if longAttachment is None else "%s (%s)" % (sentQuestion, longAttachment.name)
	conversation.addMessage(
		conversations.ROLE_USER,
		shownQuestion,
		metadata=attachments.messageMetadata(items, context, screenshot is not None),
	)
	conversation.draft = ""
	# Saved right away, so the sent question is kept and the cleared draft cannot come back after a crash.
	state.save(conversation)
	try:
		state.history.add(question)
	except Exception:
		log.debugWarning("Ask AI: could not save the question history", exc_info=True)
	state.clearPendingInput()
	pending = PendingRequest(
		makeClient(conversation=conversation),
		shownQuestion,
		conversation.id,
		conversation.projectFolder,
		providerId,
		plan,
		conversation.title,
	)
	# What the user typed, given back to the Question box if the request fails or is cancelled.
	pending.typedQuestion = question
	state.pending = pending
	threading.Thread(target=_requestWorker, args=(pending, prompt), name="askAI.ask", daemon=True).start()
	return pending


def _requestWorker(pending, prompt):
	"""Background thread. Snapshots the project folder around the request to find new files."""
	before = folderSnapshot.snapshot(pending.projectFolder) if pending.projectFolder else None
	try:
		if pending.plan is not None:
			try:
				attachments.RequestFiles.prepare(pending.plan)
			except Exception as e:
				log.error("Ask AI: could not prepare the attached files", exc_info=True)
				raise ProviderError(
					providerBase.KIND_OTHER,
					"The attached files could not be prepared.",
					str(e),
					provider=pending.providerId,
				)
			answer = pending.client.ask(prompt, pending.cancelEvent, attachments=pending.plan)
		else:
			answer = pending.client.ask(prompt, pending.cancelEvent)
	except ProviderError as e:
		if e.kind != providerBase.KIND_CANCELLED:
			log.debugWarning("Ask AI: %s: %s: %s | %s" % (pending.providerId, e.kind, e.message, e.detail))
		wx.CallAfter(_onRequestDone, pending, None, e, (), ())
		return
	except Exception as e:
		log.error("Ask AI: unexpected error", exc_info=True)
		error = ProviderError(providerBase.KIND_OTHER, "", str(e), provider=pending.providerId)
		wx.CallAfter(_onRequestDone, pending, None, error, (), ())
		return
	finally:
		# The request's private folder (copied files, screenshot) is never kept.
		if pending.plan is not None:
			attachments.RequestFiles.remove(pending.plan.directory)
	created, changed = (), ()
	if before is not None:
		try:
			created, changed = folderSnapshot.compare(before, folderSnapshot.snapshot(pending.projectFolder))
		except Exception:
			log.debugWarning("Ask AI: could not compare the project folder", exc_info=True)
	wx.CallAfter(_onRequestDone, pending, answer, None, created, changed)


def cancelRequest():
	"""Cancel the running request, if any, and take its question back out of its conversation."""
	pending = state.pending
	if pending is None:
		return None
	state.pending = None
	pending.cancelEvent.set()
	conversation = state.conversationById(pending.conversationId)
	if conversation is not None and conversation.removeLastMessage(conversations.ROLE_USER, pending.question):
		if conversation is not state.current:
			state.save(conversation)
	return pending


def statusTemplates():
	"""Translated templates for status.statusText / doneText / failedText."""
	return {
		# Translators: Status report while an answer is coming. {service} is "ChatGPT" or "Claude",
		# {title} the conversation title, {elapsed} e.g. "40 seconds".
		"answering": _("{service} is answering in {title}, {elapsed}."),
		# Translators: Status report while voice typing keeps NVDA quiet.
		"dictating": _("Dictating."),
		# Translators: Status report while a sign-in waits for the browser.
		"signingIn": _("Waiting for sign-in."),
		# Translators: Status report when the current service is signed out. {service} is "ChatGPT" or "Claude".
		"notSignedIn": _("Not signed in to {service}"),
		# Translators: Status report when nothing is running. {service} is "ChatGPT" or "Claude",
		# {time} e.g. "3:05 PM", {title} the conversation title.
		"idleWithAnswer": _("Idle. Last answer from {service} at {time} in {title}."),
		# Translators: Status report when nothing is running and there is no answer yet.
		"idle": _("Idle."),
		# Translators: A duration in seconds, e.g. "40 seconds".
		"seconds": _("{seconds} seconds"),
		# Translators: A duration of one second.
		"oneSecond": _("1 second"),
		# Translators: A duration, e.g. "2 minutes 5 seconds".
		"minutes": _("{minutes} minutes {seconds} seconds"),
		# Translators: Spoken before an answer that arrives while the user works in another app. {title} is the conversation.
		"done": _("Done: {title}."),
		# Translators: Spoken when a question fails while the user works in another app. {reason} explains why.
		"failed": _("Failed: {title}. {reason}"),
		"untitled": _("Untitled conversation"),
	}


def _dialogInForeground(dialog):
	try:
		return dialog is not None and dialog.IsActive()
	except Exception:
		return False


def _onRequestDone(pending, answer, error, created, changed):
	if pending is not state.pending:
		return  # Cancelled.
	state.pending = None
	conversation = state.conversationById(pending.conversationId)
	if conversation is None:
		log.debugWarning("Ask AI: the conversation of a finished request was deleted")
		return
	isCurrent = conversation is state.current
	dialog = AskAIDialog.getInstance()
	# Working in another app: say which conversation finished (phrases from T3 Code's announcer).
	background = getConf()["announceInOtherApps"] and not _dialogInForeground(dialog)
	templates = statusTemplates()
	title = conversation.title
	if error is None:
		conversation.addMessage(conversations.ROLE_ASSISTANT, answer, provider=pending.providerId)
		state.lastAnswerInfo = (pending.providerId, time.time(), title)
		if state.loginStates.get(pending.providerId) != providerBase.STATE_API_KEY:
			state.loginStates[pending.providerId] = providerBase.STATE_SUBSCRIPTION
		changes = fileChangesText(created, changed)
		if changes:
			conversation.addMessage(conversations.ROLE_NOTE, changes)
		state.save(conversation)
		spoken = stripMarkdown(answer) or answer
		if dialog is not None and isCurrent:
			dialog.onAnswer(spoken, changes, doneTitle=title if background else None)
		elif getConf()["speakAnswers"]:
			ui.message(statusText.doneText(title, spoken, templates) if background else spoken)
			if changes:
				ui.message(changes)
		else:
			# Translators: Announced when an answer arrives after the dialog was closed and speaking is off.
			ready = _("The answer is ready. Press NVDA+Alt+Shift+G to hear it.")
			ui.message(statusText.doneText(title, ready, templates) if background else ready)
		return
	if error.kind == providerBase.KIND_NOT_LOGGED_IN:
		state.loginStates[pending.providerId] = providerBase.STATE_SIGNED_OUT
	conversation.addMessage(conversations.ROLE_ERROR, transcriptErrorText(error))
	state.save(conversation)
	if background:
		if dialog is not None and isCurrent:
			dialog.onFailure(getattr(pending, "typedQuestion", pending.question), error, quiet=True)
		ui.message(statusText.failedText(title, friendlyError(error), templates))
	elif dialog is not None and isCurrent:
		dialog.onFailure(getattr(pending, "typedQuestion", pending.question), error)
	else:
		ui.message(friendlyError(error))


def currentStatus(now=None):
	"""The "Report Ask AI status" sentence. Uses only what the add-on already knows; runs no program."""
	conversation = state.current
	providerId = conversation.provider if conversation is not None and providers.isKnown(conversation.provider) else defaultProvider()
	pending = state.pending
	answering = None
	if pending is not None:
		answering = (serviceName(pending.providerId), pending.title, pending.startedAt)
	lastAnswer = None
	if state.lastAnswerInfo is not None:
		answerProvider, answeredAt, answerTitle = state.lastAnswerInfo
		lastAnswer = (serviceName(answerProvider), answeredAt, answerTitle)
	snapshot = statusText.StatusSnapshot(
		answering=answering,
		dictating=_activeDictation is not None,
		signingIn=_loginClient is not None,
		service=serviceName(providerId),
		signedOut=state.loginStates.get(providerId) == providerBase.STATE_SIGNED_OUT,
		lastAnswer=lastAnswer,
	)
	return statusText.statusText(snapshot, now, statusTemplates())


def switchConversation(conversation):
	"""Make conversation current (main thread). The running request keeps its own conversation.
	The Question text is kept as the old conversation's draft, and the new one's draft is restored.
	"""
	dialog = AskAIDialog.getInstance()
	if state.current is not None and state.current is not conversation:
		if dialog is not None:
			dialog.saveDraft(silent=True)
		state.save(state.current)
	state.current = conversation
	if dialog is not None:
		dialog.loadConversation(restoreDraft=True)


def cancelLogin():
	"""Stop a running sign-in synchronously. Main thread."""
	global _loginClient
	client, _loginClient = _loginClient, None
	if client is not None:
		client.cancelLogin()


def _finishLogin(client, timedOut):
	"""Main thread, after the sign-in program exited or timed out. Compare-and-clear avoids races with a
	newer sign-in attempt, sign-out or shutdown, which replace or clear _loginClient first.
	"""
	global _loginClient
	if _loginClient is not client:
		return
	_loginClient = None
	client.cancelLogin()  # Kills the process if it is still waiting, and releases its job.
	if timedOut:
		# Translators: Announced when the user did not finish signing in.
		ui.message(_("Sign-in was not completed. Press Sign in to try again."))
		_refreshPanel()
		return

	def check():
		try:
			loginState, text = client.loginStatus()
		except Exception:
			log.debugWarning("Ask AI: status check after sign-in failed", exc_info=True)
			loginState, text = providerBase.STATE_SIGNED_OUT, ""
		wx.CallAfter(_announceLoginResult, client.providerId, loginState, text)

	threading.Thread(target=check, name="askAI.loginCheck", daemon=True).start()


def _announceLoginResult(providerId, loginState, text):
	state.loginStates[providerId] = loginState
	if loginState == providerBase.STATE_API_KEY:
		# Translators: Announced when the program is signed in with an API key. {program} is e.g. "Codex".
		ui.message(_("{program} is signed in with an API key, not with your subscription.").format(program=programName(providerId)))
	elif loginState == providerBase.STATE_SUBSCRIPTION:
		# Translators: Announced when sign-in succeeded. {service} is "ChatGPT" or "Claude".
		ui.message(_("Signed in with {service}.").format(service=serviceName(providerId)))
	else:
		# Translators: Announced when sign-in did not complete.
		ui.message(_("Sign-in was not completed. Press Sign in to try again."))
	_refreshPanel()


def _refreshPanel():
	panel = _activePanel
	if panel is not None and panel:
		panel.refreshStatus()


def loginStatusText(providerId, loginState):
	"""The Status field text for a sign-in state."""
	if loginState == providerBase.STATE_API_KEY:
		# Translators: Status when the program uses an API key instead of the user's subscription.
		return _("Signed in with an API key, which is billed per use")
	if loginState == providerBase.STATE_SUBSCRIPTION:
		# Translators: Status when signed in. {service} is "ChatGPT" or "Claude".
		return _("Signed in with {service}").format(service=serviceName(providerId))
	# Translators: Status when not signed in.
	return _("Not signed in")


def restoreDictation():
	"""Give NVDA its speech back if a dictation is running. Safe to call any time."""
	global _activeDictation
	session, _activeDictation = _activeDictation, None
	if session is not None:
		try:
			session.finish(dictation.REASON_CLOSE)
		except Exception:
			log.error("Ask AI: could not restore the speech mode", exc_info=True)


def shutdown():
	"""Kill everything synchronously and restore speech. Called from GlobalPlugin.terminate()."""
	try:
		restoreDictation()
	finally:
		pending = state.pending
		if pending is not None:
			state.pending = None
			pending.cancelEvent.set()
			pending.client.terminate()
		cancelLogin()
		if state.current is not None:
			state.save(state.current)
		closeAskDialog()


class AskAIDialog(DpiScalingHelperMixinWithoutInit, wx.Dialog):
	"""Non-modal dialog for asking questions. Only one instance exists at a time.
	Mnemonics: Q A/C D T U H P I V O W S M E J F X L R N (Cancel shares the Ask button).
	Keys: Ctrl+F find, F3 / Shift+F3 next / previous match, Ctrl+S save draft, Up / Down recall questions.
	"""

	_instance = None

	@classmethod
	def getInstance(cls):
		instance = cls._instance
		if instance is not None and instance:
			return instance
		cls._instance = None
		return None

	def __init__(self, parent):
		# Translators: Title of the Ask AI dialog.
		super().__init__(parent, title=_("Ask AI"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		AskAIDialog._instance = self
		self._busy = False
		self._dictateToken = 0
		self._dictation = None
		self._lastAnswerPos = None
		self._shownEntries = []
		self._modelSlugs = []
		self._effortValues = []
		self._providerIds = providers.ids()
		self._recall = history.QuestionRecall()
		# Start of the current search match in the Conversation text, or None.
		self._matchStart = None
		self.beepTimer = wx.Timer(self)
		self.dictationTimer = wx.Timer(self)
		width = self.scaleSize(ASK_TEXT_WIDTH)

		mainSizer = wx.BoxSizer(wx.VERTICAL)
		sHelper = guiHelper.BoxSizerHelper(self, orientation=wx.VERTICAL)

		self.questionCtrl = sHelper.addLabeledControl(
			# Translators: Label of the box where the question is typed.
			_("&Question:"),
			wx.TextCtrl,
			style=wx.TE_MULTILINE,
			size=(width, self.scaleSize(70)),
		)

		actionButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: Button that sends the question.
		self.askButton = actionButtons.addButton(self, label=_("&Ask"))
		self.askButton.SetDefault()
		self.askButton.Bind(wx.EVT_BUTTON, self.onAskButton)
		# Translators: Button that starts Windows voice typing to dictate the question.
		self.dictateButton = actionButtons.addButton(self, label=_("&Dictate (Windows voice typing)"))
		self.dictateButton.Bind(wx.EVT_BUTTON, self.onDictate)
		sHelper.addItem(actionButtons)

		# Screen text attached by "Ask about this" (NVDA+Alt+A); shown only while some is attached.
		contextLabel = wx.StaticText(
			self,
			# Translators: Label of the read-only field summarising the screen text attached to the next question.
			label=_("Context:"),
		)
		self.contextCtrl = wx.TextCtrl(self, style=STATUS_STYLE, size=(width - self.scaleSize(200), -1))
		self.contextCtrl.SetInitialSize((width - self.scaleSize(200), int(self.contextCtrl.GetCharHeight() * 1.6)))
		self.removeContextButton = wx.Button(
			self,
			# Translators: Button that removes the screen text attached to the next question.
			label=_("Remove con&text"),
		)
		self.removeContextButton.Bind(wx.EVT_BUTTON, self.onRemoveContext)
		self.screenshotCheck = wx.CheckBox(
			self,
			# Translators: Checkbox to send a picture of the window together with the attached screen text.
			label=_("Incl&ude a screenshot of the window"),
		)
		self.screenshotCheck.Bind(wx.EVT_CHECKBOX, self.onScreenshotCheck)
		contextRow = wx.BoxSizer(wx.HORIZONTAL)
		contextRow.Add(contextLabel, flag=wx.ALIGN_CENTER_VERTICAL)
		contextRow.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_HORIZONTAL)
		contextRow.Add(self.contextCtrl, proportion=1, flag=wx.ALIGN_CENTER_VERTICAL)
		contextRow.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_HORIZONTAL)
		contextRow.Add(self.removeContextButton, flag=wx.ALIGN_CENTER_VERTICAL)
		self.contextBlock = wx.BoxSizer(wx.VERTICAL)
		self.contextBlock.Add(contextRow, flag=wx.EXPAND)
		self.contextBlock.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_VERTICAL)
		self.contextBlock.Add(self.screenshotCheck)
		sHelper.addItem(self.contextBlock, flag=wx.EXPAND)

		# Files attached to the next question.
		attachRow = wx.BoxSizer(wx.HORIZONTAL)
		self.attachButton = wx.Button(
			self,
			# Translators: Button that attaches files (documents, images) to the next question.
			label=_("Attac&h files..."),
		)
		self.attachButton.Bind(wx.EVT_BUTTON, self.onAttachFiles)
		attachmentsLabel = wx.StaticText(
			self,
			# Translators: Label of the list of files attached to the next question.
			label=_("&Pending attachments:"),
		)
		self.attachmentsList = wx.ListBox(self, size=(self.scaleSize(260), self.scaleSize(50)))
		self.removeAttachmentButton = wx.Button(
			self,
			# Translators: Button that removes the selected file from the attachments.
			label=_("Remove f&ile"),
		)
		self.removeAttachmentButton.Bind(wx.EVT_BUTTON, self.onRemoveAttachment)
		attachRow.Add(self.attachButton, flag=wx.ALIGN_CENTER_VERTICAL)
		attachRow.AddSpacer(guiHelper.SPACE_BETWEEN_BUTTONS_HORIZONTAL)
		attachRow.Add(attachmentsLabel, flag=wx.ALIGN_CENTER_VERTICAL)
		attachRow.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_HORIZONTAL)
		attachRow.Add(self.attachmentsList, proportion=1)
		attachRow.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_HORIZONTAL)
		attachRow.Add(self.removeAttachmentButton, flag=wx.ALIGN_CENTER_VERTICAL)
		sHelper.addItem(attachRow, flag=wx.EXPAND)

		self.conversationCtrl = sHelper.addLabeledControl(
			# Translators: Label of the read-only box showing the conversation.
			_("Con&versation:"),
			wx.TextCtrl,
			style=wx.TE_MULTILINE | wx.TE_READONLY,
			size=(width, self.scaleSize(220)),
		)

		answerButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: Button that copies the last answer to the clipboard.
		copyButton = answerButtons.addButton(self, label=_("C&opy last answer"))
		copyButton.Bind(wx.EVT_BUTTON, self.onCopy)
		# Translators: Button that saves the last answer as a Word, PDF or other file.
		self.saveButton = answerButtons.addButton(self, label=_("Save ans&wer as..."))
		self.saveButton.Bind(wx.EVT_BUTTON, self.onSaveAnswer)
		sHelper.addItem(answerButtons)

		self.serviceChoice = sHelper.addLabeledControl(
			# Translators: Label of the choice of AI service (ChatGPT or Claude) for this conversation.
			_("AI &service:"),
			wx.Choice,
			choices=[providers.displayName(pid) for pid in self._providerIds],
		)
		self.serviceChoice.Bind(wx.EVT_CHOICE, self.onServiceChoice)

		modelRow = guiHelper.BoxSizerHelper(self, orientation=wx.HORIZONTAL)
		self.modelChoice = modelRow.addLabeledControl(
			# Translators: Label of the model choice for this conversation.
			_("&Model:"),
			wx.Choice,
			choices=[],
		)
		self.modelChoice.Bind(wx.EVT_CHOICE, self.onModelChoice)
		self.effortChoice = modelRow.addLabeledControl(
			# Translators: Label of the reasoning effort choice for this conversation.
			_("Reasoning &effort:"),
			wx.Choice,
			choices=[],
		)
		self.effortChoice.Bind(wx.EVT_CHOICE, self.onEffortChoice)
		sHelper.addItem(modelRow)

		self.projectCtrl = sHelper.addLabeledControl(
			# Translators: Label of the read-only field showing this conversation's project folder.
			_("Pro&ject folder:"),
			wx.TextCtrl,
			style=STATUS_STYLE,
			size=(width - self.scaleSize(120), -1),
		)
		# One line high (see STATUS_STYLE).
		self.projectCtrl.SetInitialSize((width - self.scaleSize(120), int(self.projectCtrl.GetCharHeight() * 1.6)))
		projectButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: Button to choose the project folder where the AI may read and save files.
		chooseProjectButton = projectButtons.addButton(self, label=_("Choose project &folder..."))
		chooseProjectButton.Bind(wx.EVT_BUTTON, self.onChooseProject)
		self.chooseProjectButton = chooseProjectButton
		# Translators: Button that opens the project folder in Windows Explorer.
		self.openProjectButton = projectButtons.addButton(self, label=_("Open folder in E&xplorer"))
		self.openProjectButton.Bind(wx.EVT_BUTTON, self.onOpenProject)
		# Translators: Button that removes the project folder from this conversation.
		self.clearProjectButton = projectButtons.addButton(self, label=_("C&lear project folder"))
		self.clearProjectButton.Bind(wx.EVT_BUTTON, self.onClearProject)
		sHelper.addItem(projectButtons)

		bottomButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: Button that opens the list of saved conversations.
		conversationsButton = bottomButtons.addButton(self, label=_("Conve&rsations..."))
		conversationsButton.Bind(wx.EVT_BUTTON, self.onConversations)
		# Translators: Button that starts a new conversation (the current one stays saved).
		newButton = bottomButtons.addButton(self, label=_("&New conversation"))
		newButton.Bind(wx.EVT_BUTTON, self.onNewConversation)
		# Translators: Button that closes the dialog.
		closeButton = bottomButtons.addButton(self, id=wx.ID_CLOSE, label=_("Close"))
		closeButton.Bind(wx.EVT_BUTTON, lambda evt: self.Close())
		sHelper.addDialogDismissButtons(bottomButtons, separated=True)

		mainSizer.Add(sHelper.sizer, proportion=1, border=guiHelper.BORDER_FOR_DIALOGS, flag=wx.ALL | wx.EXPAND)
		self.SetSizer(mainSizer)
		mainSizer.Fit(self)
		self.CentreOnScreen()

		self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)
		self.Bind(wx.EVT_CLOSE, self.onClose)
		self.Bind(wx.EVT_TIMER, self.onBeepTimer, self.beepTimer)
		self.Bind(wx.EVT_TIMER, self.onDictationTimer, self.dictationTimer)
		state.reloadModels()
		self.loadConversation(restoreDraft=True)
		self.updatePendingInput()
		if state.pending is not None:
			self._setBusy(True)  # A question asked before the dialog was closed is still running.
		self.questionCtrl.SetFocus()

	# Conversation

	@property
	def conversation(self):
		return state.ensureCurrent()

	@property
	def providerId(self):
		providerId = self.conversation.provider
		return providerId if providers.isKnown(providerId) else providers.DEFAULT_PROVIDER

	def loadConversation(self, restoreDraft=False):
		"""Show the current conversation: title, Conversation box, service, model, effort and project folder.
		With restoreDraft (opening or continuing it), its saved draft goes into an empty Question box.
		"""
		conversation = self.conversation
		self._matchStart = None
		self._recall.reset()
		if restoreDraft and conversation.draft and not self.questionCtrl.GetValue().strip():
			self.questionCtrl.SetValue(conversation.draft)
			self.questionCtrl.SetInsertionPointEnd()
			# Translators: Announced when a conversation opens with its unsent question.
			wx.CallLater(ANNOUNCE_AFTER_FOCUS_MS * 3, ui.message, _("Draft restored"))
		self._updateTitle()
		self._shownEntries = []
		self._refreshConversationBox(rebuild=True)
		if self._lastAnswerPos is not None:
			# Opening or switching lands on the beginning of the newest answer.
			self.conversationCtrl.SetInsertionPoint(self._lastAnswerPos)
			self.conversationCtrl.ShowPosition(self._lastAnswerPos)
		self.serviceChoice.SetSelection(self._providerIds.index(self.providerId))
		self._fillModelChoices()
		self._updateProject()
		return conversation

	def _updateTitle(self):
		title = self.conversation.title
		# Translators: Title of the Ask AI dialog with the conversation title, e.g. "Ask AI - Trip to Paris".
		self.SetTitle(_("Ask AI - {title}").format(title=title) if title else _("Ask AI"))

	def _refreshConversationBox(self, rebuild=False):
		"""Mirror the conversation into the box: append new entries, or rebuild when old ones changed."""
		ctrl = self.conversationCtrl
		entries = transcriptEntries(self.conversation)
		hasFocus = self.FindFocus() == ctrl
		caret = ctrl.GetInsertionPoint()
		shown = self._shownEntries
		rebuilt = rebuild or entries[: len(shown)] != shown
		if rebuilt:
			ctrl.Clear()
			shown = []
			self._lastAnswerPos = None
		appendedAnswerPos = None
		for text, isAnswer in entries[len(shown):]:
			if ctrl.GetLastPosition() > 0:
				ctrl.AppendText("\n\n")
			if isAnswer:
				# Measured after appending the separator, so it is right however the control counts line breaks.
				appendedAnswerPos = ctrl.GetLastPosition()
			ctrl.AppendText(text)
		if appendedAnswerPos is not None:
			self._lastAnswerPos = appendedAnswerPos
		self._shownEntries = list(entries)
		if hasFocus:
			# Never move a caret the user is using; after a rebuild keep it near where it was.
			if rebuilt:
				ctrl.SetInsertionPoint(min(caret, ctrl.GetLastPosition()))
			return
		if appendedAnswerPos is not None and not rebuilt:
			# Moving to the conversation then lands on the newest answer.
			ctrl.SetInsertionPoint(appendedAnswerPos)
			ctrl.ShowPosition(appendedAnswerPos)

	def _models(self):
		return state.modelsFor(self.providerId)

	def _fillModelChoices(self):
		conversation = self.conversation
		providerClass = providers.get(self.providerId)
		_path, defaultModel, _effort = providerSettings(self.providerId)
		models = modelCatalog.modelChoices(
			self._models(),
			extraSlugs=[conversation.model, defaultModel],
			efforts=providerClass.defaultEfforts,
		)
		self._modelSlugs = [""] + [model.slug for model in models]
		self.modelChoice.Set([defaultModelLabel()] + [model.displayName for model in models])
		self.modelChoice.SetSelection(self._modelSlugs.index(conversation.model) if conversation.model in self._modelSlugs else 0)
		self._fillEffortChoices()

	def _fillEffortChoices(self):
		conversation = self.conversation
		fallback = providers.get(self.providerId).defaultEfforts
		efforts = modelCatalog.effortsFor(self._models(), conversation.model, fallback)
		wanted = conversation.reasoningEffort or providerSettings(self.providerId)[2]
		effort = modelCatalog.pickEffort(self._models(), conversation.model, wanted, fallback)
		self._effortValues = list(efforts)
		self.effortChoice.Set([effortLabel(value) for value in efforts])
		self.effortChoice.SetSelection(self._effortValues.index(effort) if effort in self._effortValues else 0)

	def _modelName(self, slug):
		model = modelCatalog.findModel(self._models(), slug) if slug else None
		if model is not None:
			return model.displayName
		return slug or defaultModelLabel()

	def onServiceChoice(self, evt):
		selection = self.serviceChoice.GetSelection()
		if selection < 0:
			return
		providerId = self._providerIds[selection]
		conversation = self.conversation
		if providerId == conversation.provider:
			return
		_path, model, effort = providerSettings(providerId)
		conversation.provider = providerId
		conversation.model = model
		conversation.reasoningEffort = effort
		conversation.touch()
		self._fillModelChoices()
		state.save(conversation)
		# Translators: Announced when the conversation switches service. {service} is "ChatGPT" or "Claude".
		wx.CallLater(ANNOUNCE_AFTER_FOCUS_MS, ui.message, _("Now using {service}").format(service=serviceName(providerId)))

	def onModelChoice(self, evt):
		selection = self.modelChoice.GetSelection()
		if selection < 0:
			return
		conversation = self.conversation
		slug = self._modelSlugs[selection]
		if slug == conversation.model:
			return
		conversation.model = slug
		conversation.reasoningEffort = modelCatalog.pickEffort(
			self._models(),
			slug,
			conversation.reasoningEffort or providerSettings(self.providerId)[2],
			providers.get(self.providerId).defaultEfforts,
		)
		conversation.touch()
		self._fillEffortChoices()
		state.save(conversation)
		# Translators: Announced when the model changes. {model} is e.g. "GPT 6.1 Sol".
		wx.CallLater(ANNOUNCE_AFTER_FOCUS_MS, ui.message, _("Model changed to {model}").format(model=self._modelName(slug)))

	def onEffortChoice(self, evt):
		selection = self.effortChoice.GetSelection()
		if selection < 0:
			return
		self.conversation.reasoningEffort = self._effortValues[selection]
		self.conversation.touch()
		state.save(self.conversation)

	def _updateProject(self):
		folder = self.conversation.projectFolder
		# Translators: Shown in the project folder field when no folder is set.
		self.projectCtrl.SetValue(folder or _("None"))
		self.openProjectButton.Enable(bool(folder))
		self.clearProjectButton.Enable(bool(folder))

	def onChooseProject(self, evt):
		conversation = self.conversation
		start = conversation.projectFolder if os.path.isdir(conversation.projectFolder or "") else documentsFolder()
		with wx.DirDialog(
			self,
			# Translators: Title of the folder chooser for the project folder.
			_("Choose the project folder for this conversation"),
			defaultPath=start if os.path.isdir(start) else "",
			style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST,
		) as folderDialog:
			if folderDialog.ShowModal() != wx.ID_OK:
				return
			folder = folderDialog.GetPath()
		conversation.projectFolder = folder
		conversation.touch()
		state.save(conversation)
		self._updateProject()
		name = os.path.basename(os.path.normpath(folder)) or folder
		# Translators: Announced after choosing the project folder. {name} is the folder name.
		wx.CallLater(ANNOUNCE_AFTER_FOCUS_MS, ui.message, _("Project folder: {name}").format(name=name))

	def onOpenProject(self, evt):
		folder = self.conversation.projectFolder
		if not folder:
			return
		if not os.path.isdir(folder):
			# Translators: Error when the project folder was moved or deleted. {path} is the folder.
			showError(_("The project folder no longer exists: {path}. Choose another folder or clear it.").format(path=folder), self)
			return
		try:
			os.startfile(folder)
		except Exception as e:
			log.error("Ask AI: could not open the project folder", exc_info=True)
			# Translators: Error when Explorer could not open the folder. {error} is the reason.
			showError(_("Could not open the folder: {error}").format(error=e), self)

	def onClearProject(self, evt):
		conversation = self.conversation
		conversation.projectFolder = ""
		conversation.touch()
		state.save(conversation)
		self._updateProject()
		# This button is now disabled; move focus to a control that still works.
		# Translators: Announced after removing the project folder.
		self._announceAfterFocus(self.chooseProjectButton, _("Project folder cleared"))

	def onConversations(self, evt):
		from .conversationManager import showConversationManager

		showConversationManager(self)

	def onNewConversation(self, evt):
		if self._dictation is not None:
			self._finishDictation(dictation.REASON_CLOSE)
		if state.current is not None and not state.current.isEmpty:
			state.save(state.current)
		switchConversation(state.newConversation())
		self.questionCtrl.Clear()
		# Translators: Announced when a new conversation starts.
		self._announceAfterFocus(self.questionCtrl, _("New conversation started."))

	# Drafts

	def saveDraft(self, silent=False):
		"""Keep the Question text as the conversation's draft (an empty box clears it)."""
		if not self:
			return
		conversation = self.conversation
		text = self.questionCtrl.GetValue()
		if self._recall.shown is not None and text == self._recall.shown:
			text = self._recall.typed  # A recalled earlier question is not a draft.
		if text.strip() == conversation.draft.strip():
			if not silent:
				ui.message(_("Draft saved"))
			return
		conversation.draft = text if text.strip() else ""
		conversation.touch()
		state.save(conversation)
		if not silent:
			# Translators: Announced after Ctrl+S saves the Question text as the conversation's draft.
			ui.message(_("Draft saved"))

	# Attached context and files

	def updatePendingInput(self):
		"""Show the attached screen text and files (the Context row only while there is context)."""
		if not self:
			return
		context = state.pendingContext
		self.contextCtrl.SetValue(context.summary(contextSummaryTemplate()) if context is not None else "")
		self.screenshotCheck.SetValue(state.includeScreenshot)
		self.screenshotCheck.Enable(context is not None and context.screenshot is not None)
		self.contextBlock.ShowItems(context is not None)
		items = [attachmentLabel(item) for item in state.pendingAttachments]
		self.attachmentsList.Set(items)
		if items:
			self.attachmentsList.SetSelection(0)
		self.removeAttachmentButton.Enable(bool(items))
		self.Layout()
		self.GetSizer().Fit(self)

	def attachContext(self, context):
		"""Main thread: context from "Ask about this" applies to the next question."""
		state.pendingContext = context
		state.includeScreenshot = False
		self.updatePendingInput()
		where = context.appName or context.windowTitle
		# Translators: Announced after "Ask about this". {app} is the application, e.g. "Microsoft Word".
		self._announceAfterFocus(self.questionCtrl, _("Context attached from {app}. Type your question.").format(app=where))

	def onRemoveContext(self, evt):
		state.pendingContext = None
		state.includeScreenshot = False
		self.updatePendingInput()
		# Translators: Announced after the attached screen text was removed.
		self._announceAfterFocus(self.questionCtrl, _("Context removed"))

	def onScreenshotCheck(self, evt):
		state.includeScreenshot = self.screenshotCheck.GetValue()

	def onAttachFiles(self, evt):
		with wx.FileDialog(
			self,
			# Translators: Title of the file chooser for attaching files to the next question.
			_("Attach files to the next question"),
			# Translators: File type filter for attachments.
			wildcard=_("All files (*.*)|*.*"),
			style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE,
		) as fileDialog:
			if fileDialog.ShowModal() != wx.ID_OK:
				return
			paths = fileDialog.GetPaths()
		added, problems = self.attachPaths(paths)
		if problems:
			showError("\n".join(problems), self)
		if added:
			# Translators: Announced after attaching files. {count} is a number.
			message = ngettext("{count} file attached", "{count} files attached", added).format(count=added)
			self._announceAfterFocus(self.attachmentsList, message)

	def attachPaths(self, paths):
		"""Attach files; returns (number added, [problem messages])."""
		added = 0
		problems = []
		for path in paths:
			try:
				state.pendingAttachments.add(path)
				added += 1
			except attachments.AttachmentError as e:
				problems.append(attachmentProblem(e))
		self.updatePendingInput()
		return added, problems

	def onRemoveAttachment(self, evt):
		index = self.attachmentsList.GetSelection()
		if index < 0 or index >= len(state.pendingAttachments):
			return
		item = state.pendingAttachments.remove(index)
		self.updatePendingInput()
		if len(state.pendingAttachments):
			self.attachmentsList.SetSelection(min(index, len(state.pendingAttachments) - 1))
			target = self.attachmentsList
		else:
			target = self.attachButton
		# Translators: Announced after removing an attachment. {name} is the file name.
		self._announceAfterFocus(target, _("Removed {name}").format(name=item.name))

	# Search in the Conversation box

	def onFind(self):
		with wx.TextEntryDialog(
			self,
			# Translators: Prompt of the find dialog for the Conversation box.
			_("Find in the conversation:"),
			# Translators: Title of the find dialog.
			_("Find"),
			state.lastSearch,
		) as findDialog:
			if findDialog.ShowModal() != wx.ID_OK:
				return
			query = findDialog.GetValue().strip()
		if not query:
			return
		state.lastSearch = query
		self._matchStart = None
		self.findNext(forward=True, fromCaret=True)

	def findNext(self, forward=True, fromCaret=False):
		"""Select the next (or previous) match in the Conversation box, focus it and say "Match 2 of 5"."""
		query = state.lastSearch
		if not query:
			self.onFind()
			return
		ctrl = self.conversationCtrl
		value = ctrl.GetValue()
		matches = search.findMatches(value, query)
		if not matches:
			# Translators: Reported when the search finds nothing. {query} is the search text.
			ui.message(_("No matches for {query}").format(query=query))
			return
		wrapped = False
		if fromCaret or self._matchStart is None:
			index = search.nearestMatch(matches, 0, forward=True) if fromCaret else (0 if forward else len(matches) - 1)
		elif forward:
			later = [i for i, start in enumerate(matches) if start > self._matchStart]
			index, wrapped = (later[0], False) if later else (0, True)
		else:
			earlier = [i for i, start in enumerate(matches) if start < self._matchStart]
			index, wrapped = (earlier[-1], False) if earlier else (len(matches) - 1, True)
		start = matches[index]
		self._matchStart = start
		lastPosition = ctrl.GetLastPosition()
		begin = search.controlPosition(value, start, lastPosition)
		end = search.controlPosition(value, start + len(query), lastPosition)
		ctrl.SetFocus()
		ctrl.SetSelection(begin, end)
		ctrl.ShowPosition(begin)
		parts = []
		if wrapped:
			# Translators: Announced when the search goes past the last (or first) match and starts again.
			parts.append(_("Wrapped to first match.") if forward else _("Wrapped to last match."))
		# Translators: Announced for a search match. {number} and {count} are numbers, {line} is the matched line.
		parts.append(_("Match {number} of {count}.").format(number=index + 1, count=len(matches)))
		parts.append(search.lineAt(value, start))
		wx.CallLater(ANNOUNCE_AFTER_FOCUS_MS, ui.message, " ".join(parts))

	# Question recall

	def recallQuestion(self, older):
		value = self.questionCtrl.GetValue()
		entries = history.recallList(self.conversation.questions, state.history.entries)
		text, result = self._recall.move(value, entries, older)
		if text is not None:
			self.questionCtrl.SetValue(text)
			self.questionCtrl.SetInsertionPointEnd()
		if result == history.NO_EARLIER:
			# Translators: Announced when Up has no earlier question to recall.
			ui.message(_("No earlier question"))
		elif result == history.NO_LATER:
			# Translators: Announced when Down goes past the newest recalled question.
			ui.message(_("No later question"))
		elif text is not None:
			ui.message(text)

	# Keyboard

	def onCharHook(self, evt):
		key = evt.GetKeyCode()
		control, shift, alt = evt.ControlDown(), evt.ShiftDown(), evt.AltDown()
		if control and not alt and not shift and key in (ord("F"), ord("f")):
			self.onFind()
			return
		if key == wx.WXK_F3 and not control and not alt:
			self.findNext(forward=not shift)
			return
		if control and not alt and not shift and key in (ord("S"), ord("s")):
			self.saveDraft()
			return
		if (
			key in (wx.WXK_UP, wx.WXK_DOWN)
			and not (control or shift or alt)
			and self.FindFocus() == self.questionCtrl
			and self._recall.canRecall(self.questionCtrl.GetValue())
		):
			self.recallQuestion(older=key == wx.WXK_UP)
			return
		if key == wx.WXK_ESCAPE:
			if self._dictation is not None:
				self._finishDictation(dictation.REASON_ESCAPE)
			elif state.pending is not None:
				self.cancel(announce=True)
			else:
				self.Close()
			return
		if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and self.FindFocus() == self.questionCtrl:
			if evt.ShiftDown():
				self.questionCtrl.WriteText("\n")
			elif not evt.AltDown():
				if self._dictation is not None:
					# Speech comes back first, so "Asking ..." is heard.
					self._finishDictation(dictation.REASON_SEND)
				self.ask()
			return
		evt.Skip()

	def _announceAfterFocus(self, control, message):
		control.SetFocus()
		wx.CallLater(ANNOUNCE_AFTER_FOCUS_MS, ui.message, message)

	# Asking

	def onAskButton(self, evt):
		if state.pending is not None:
			self.cancel(announce=True)
		else:
			if self._dictation is not None:
				self._finishDictation(dictation.REASON_SEND)
			self.ask()

	def ask(self):
		if state.pending is not None:
			# Translators: Reported when the user tries to send while an answer is pending.
			ui.message(_("Still waiting for the answer. Press Escape to cancel."))
			return
		question = self.questionCtrl.GetValue().strip()
		if not question:
			# Translators: Reported when the user sends an empty question.
			ui.message(_("Type or dictate a question first."))
			self.questionCtrl.SetFocus()
			return
		longText = len(question) > attachments.LONG_QUESTION_CHARS
		startRequest(question, self.conversation)
		self._recall.reset()
		self._updateTitle()
		self._refreshConversationBox()
		self.questionCtrl.Clear()
		self.updatePendingInput()
		self._setBusy(True)
		if longText:
			# Translators: Announced when a very long question is sent as an attached file.
			ui.message(_("Long text attached as a file"))
		# Translators: Announced while waiting for an answer. {service} is "ChatGPT" or "Claude".
		ui.message(_("Asking {service}…").format(service=serviceName(self.providerId)))

	def onAnswer(self, spoken, changes, doneTitle=None):
		"""Show and speak an answer. doneTitle (the user is in another app) prefixes "Done: <title>."."""
		self._setBusy(False)
		self._refreshConversationBox()
		if getConf()["speakAnswers"]:
			message = spoken
		else:
			# Translators: Announced when an answer arrives and automatic speaking is off.
			message = _("Answer received. It is in the conversation box.")
		if doneTitle is not None:
			message = statusText.doneText(doneTitle, message, statusTemplates())
		ui.message(message)
		if changes:
			ui.message(changes)

	def onFailure(self, question, error, quiet=False):
		"""Show a failure. quiet (the user is in another app): no dialog; the caller speaks "Failed: ...".
		"""
		self._setBusy(False)
		self._refreshConversationBox()
		if not self.questionCtrl.GetValue().strip():
			self.questionCtrl.SetValue(question)
			self.questionCtrl.SetInsertionPointEnd()
		if quiet:
			return
		providerId = getattr(error, "provider", "") or self.providerId
		if error.kind == providerBase.KIND_NOT_LOGGED_IN:
			# Translators: Yes/No question after a sign-in error. {program} is e.g. "Codex", {service} "ChatGPT".
			offer = _("{program} is not signed in to {service}; open the Ask AI settings to sign in?").format(
				program=programName(providerId),
				service=serviceName(providerId),
			)
		elif error.kind == providerBase.KIND_NOT_INSTALLED:
			# Translators: Yes/No question after the program could not be started. {program} is e.g. "Codex".
			offer = _("The {program} program could not be started; open the Ask AI settings?").format(
				program=programName(providerId),
			)
		else:
			showError(friendlyError(error), self)
			return
		if askYesNo(offer, _("Ask AI"), self):
			openSettings(providerId)

	def cancel(self, announce=False):
		pending = cancelRequest()
		if pending is None:
			return
		self._setBusy(False)
		self._refreshConversationBox()
		if not self.questionCtrl.GetValue().strip():
			self.questionCtrl.SetValue(getattr(pending, "typedQuestion", pending.question))
			self.questionCtrl.SetInsertionPointEnd()
		if announce:
			# Translators: Announced when the user cancels a question.
			self._announceAfterFocus(self.questionCtrl, _("Request cancelled."))

	def _setBusy(self, busy):
		self._busy = busy
		if busy:
			# Translators: Label of the Ask button while waiting; pressing it cancels.
			self.askButton.SetLabel(_("&Cancel"))
			if getConf()["beepWhileWaiting"]:
				self.beepTimer.Start(BEEP_INTERVAL_MS)
		else:
			self.beepTimer.Stop()
			self.askButton.SetLabel(_("&Ask"))

	def onBeepTimer(self, evt):
		if self._busy:
			tones.beep(440, 40, left=20, right=20)

	# Answer actions

	def onCopy(self, evt):
		answer = state.lastAnswer
		if not answer:
			# Translators: Reported when there is no answer to copy.
			ui.message(_("There is no answer yet."))
			return
		if api.copyToClip(answer):
			# Translators: Reported after copying the answer.
			ui.message(_("Last answer copied to the clipboard."))
		else:
			# Translators: Reported when copying failed.
			ui.message(_("Could not copy to the clipboard."))

	def onSaveAnswer(self, evt):
		conversation = self.conversation
		index = conversation.lastAnswerIndex
		if index is None:
			ui.message(_("There is no answer yet."))
			return
		raw = conversation.messages[index]["text"]
		question = conversation.questionFor(index)
		# Translators: Title of the dialogs for saving an answer.
		chosen = chooseFormatAndName(self, exporters.fileNameFromText(question), _("Save answer as"))
		if chosen is None:
			return
		formatKey, baseName = chosen
		folder = conversation.projectFolder if os.path.isdir(conversation.projectFolder or "") else documentsFolder()
		title = question.strip().splitlines()[0] if question.strip() else conversation.title
		saveDocument(
			self,
			folder,
			formatKey,
			baseName,
			title,
			[("text", stripMarkdown(raw) or raw)],
			markdownBlocks=[("text", raw)],
		)

	# Voice input

	def onDictate(self, evt):
		if self._dictation is not None:
			# Second press: stop voice typing (Windows+H toggles it) and give speech back.
			self._sendVoiceTypingKey(checkFocus=False)
			self._finishDictation(dictation.REASON_TOGGLE)
			return
		# The button stays enabled while busy so focus never lands on a disabled control.
		if state.pending is not None:
			# Translators: Reported when Dictate is pressed while waiting for an answer.
			ui.message(_("Still waiting for the answer. Press Escape to cancel first."))
			return
		self.questionCtrl.SetFocus()
		self._dictateToken += 1
		# Let the focus announcement finish before speaking.
		wx.CallLater(250, self._announceDictation, self._dictateToken)

	def _announceDictation(self, token):
		if not self or token != self._dictateToken:
			return
		# Translators: Announced before Windows voice typing opens.
		message = _("Voice typing. Speak your question, then press Enter to send.")

		def startVoiceTyping():
			if not self or token != self._dictateToken:
				return
			self._dictateToken += 1
			# Let focus settle before opening voice typing.
			wx.CallLater(300, self._startDictation)

		# Open voice typing only after the announcement, so the microphone does not hear NVDA.
		speakThen(message, startVoiceTyping)

	def _focusInQuestion(self):
		return self.IsActive() and self.FindFocus() == self.questionCtrl

	def _startDictation(self):
		"""Mute NVDA (keeping the saved mode), then press Windows+H. Speech is restored if anything fails."""
		global _activeDictation
		if not self:
			return
		if not self._focusInQuestion():
			# Translators: Reported when voice typing was not started because focus moved.
			ui.message(_("Voice typing was not started because the focus left the question box."))
			return
		import speech

		session = dictation.DictationSession(
			dictation.SpeechModeAdapter(speech),
			silenceTimeout=getConf()["dictationSilenceTimeout"],
		)
		try:
			muted = session.start(self.questionCtrl.GetValue())
		except Exception:
			log.error("Ask AI: could not switch speech off for dictation", exc_info=True)
			muted = False
		if muted:
			self._dictation = session
			_activeDictation = session
		try:
			sent = self._sendVoiceTypingKey(checkFocus=False)
		except Exception:
			sent = False
		if not sent:
			self._finishDictation(dictation.REASON_CLOSE)
			return
		if muted:
			self.dictationTimer.Start(DICTATION_POLL_MS)

	def onDictationTimer(self, evt):
		session = self._dictation
		if session is None:
			self.dictationTimer.Stop()
			return
		try:
			reason = session.poll(self.questionCtrl.GetValue(), self._focusInQuestion())
		except Exception:
			log.error("Ask AI: dictation check failed", exc_info=True)
			reason = dictation.REASON_CLOSE
		if reason is not None:
			self._finishDictation(reason)

	def _finishDictation(self, reason):
		"""Restore speech (exactly once) and say what was dictated, unless sending or closing."""
		global _activeDictation
		session, self._dictation = self._dictation, None
		self.dictationTimer.Stop()
		if session is None:
			return
		if _activeDictation is session:
			_activeDictation = None
		try:
			restored = session.finish(reason)
		except Exception:
			log.error("Ask AI: could not restore the speech mode", exc_info=True)
			return
		if not restored or reason in (dictation.REASON_SEND, dictation.REASON_CLOSE):
			return
		dictated = session.dictatedText(self.questionCtrl.GetValue())
		if dictated:
			# Translators: Announced when dictation ends. {text} is what was dictated.
			message = _("Dictation finished. {text}").format(text=dictated)
		else:
			# Translators: Announced when dictation ends without any text.
			message = _("Dictation finished, nothing was typed")
		ui.message(message)

	def _sendVoiceTypingKey(self, checkFocus=True):
		"""Press Windows+H. Returns True if sent."""
		if not self:
			return False
		if checkFocus and not self._focusInQuestion():
			ui.message(_("Voice typing was not started because the focus left the question box."))
			return False
		try:
			import keyboardHandler

			keyboardHandler.KeyboardInputGesture.fromName("windows+h").send()
			return True
		except Exception:
			log.error("Ask AI: could not send Windows+H", exc_info=True)
			# Translators: Reported when voice typing could not be started.
			ui.message(_("Could not start Windows voice typing. Press Windows+H yourself."))
			return False

	# Closing

	def onClose(self, evt):
		self._finishDictation(dictation.REASON_CLOSE)
		try:
			self.saveDraft(silent=True)
		except Exception:
			log.debugWarning("Ask AI: could not save the draft", exc_info=True)
		pending = state.pending
		if pending is not None:
			service = serviceName(pending.providerId)
			if getConf()["speakAnswers"]:
				# Translators: Announced when the dialog is closed while waiting. {service} is "ChatGPT" or "Claude".
				ui.message(_("{service} will speak the answer when it's ready.").format(service=service))
			else:
				# Translators: Announced when the dialog is closed while waiting and speaking is off.
				ui.message(_("{service} is still working. You will hear when the answer is ready.").format(service=service))
		if state.current is not None:
			state.save(state.current)
		self._dictateToken += 1
		self.beepTimer.Stop()
		self.dictationTimer.Stop()
		if AskAIDialog._instance is self:
			AskAIDialog._instance = None
		self.Destroy()


def openAskDialog():
	"""Open the dialog, or bring the existing one to the front. Call on the main thread."""
	dialog = AskAIDialog.getInstance()
	gui.mainFrame.prePopup()
	try:
		if dialog is None:
			dialog = AskAIDialog(gui.mainFrame)
			dialog.Show()
		else:
			dialog.Raise()
		dialog.questionCtrl.SetFocus()
	finally:
		gui.mainFrame.postPopup()


def openAskDialogWithContext(screenContext):
	"""Open (or raise) the Ask dialog on the current conversation with screen text attached. Main thread."""
	openAskDialog()
	dialog = AskAIDialog.getInstance()
	if dialog is not None:
		dialog.attachContext(screenContext)
	else:
		state.pendingContext = screenContext


def closeAskDialog():
	dialog = AskAIDialog.getInstance()
	if dialog is not None:
		dialog.Close()


def openConversationManager():
	"""Open the conversation manager from anywhere (Tools menu or gesture). Main thread."""
	from .conversationManager import showConversationManager

	dialog = AskAIDialog.getInstance()
	if dialog is not None:
		dialog.Raise()
		showConversationManager(dialog)
		return
	gui.mainFrame.prePopup()
	try:
		showConversationManager(gui.mainFrame)
	finally:
		gui.mainFrame.postPopup()


# The service whose account the settings panel shows when it next opens (set before opening it).
_settingsProvider = None


def openSettings(providerId=None):
	"""Open NVDA Settings on the Ask AI panel, showing providerId's account (default: the default service)."""
	global _settingsProvider
	_settingsProvider = providerId
	gui.mainFrame.popupSettingsDialog(NVDASettingsDialog, AskAISettingsPanel)


class AskAISettingsPanel(SettingsPanel):
	# Mnemonics used here: V, G, S, H, T, Y, X, B, M, R, I, P, K, W, J, N, D, F, E, L, U.
	# The NVDA Settings dialog itself owns O (OK), C (Cancel, Categories) and A (Apply).
	# "Account settings for" switches the account controls (status, sign-in buttons, program path,
	# default model and effort) between the services, so each control keeps one name and one letter.

	# Translators: Title of the Ask AI category in NVDA settings.
	title = _("Ask AI")
	# Translators: Description of the Ask AI settings, read when the category opens.
	panelDescription = _(
		"Ask AI uses your own ChatGPT or Claude subscription through the official OpenAI Codex CLI "
		"or Anthropic's Claude Code, so no API key or pay-per-use billing is needed. "
		"Choose a service in Account settings for, then sign in."
	)

	def makeSettings(self, settingsSizer):
		global _activePanel, _settingsProvider
		_activePanel = self
		self.Bind(wx.EVT_WINDOW_DESTROY, self._onDestroy)
		conf = getConf()
		self._statusGeneration = 0
		self._loginUrl = None
		self._loginUrlProvider = None
		self._initialStatusPending = True
		self._providerIds = providers.ids()
		self._models = {}
		for providerId in self._providerIds:
			self._models[providerId] = state.reloadModels(providerId)
		# The account values being edited, per service, so switching services keeps unsaved edits.
		self._values = {}
		for providerId in self._providerIds:
			path, model, effort = providerSettings(providerId)
			self._values[providerId] = {"path": path, "model": model, "effort": effort}
		shown = _settingsProvider if providers.isKnown(_settingsProvider) else defaultProvider()
		_settingsProvider = None
		self._shownProvider = shown
		sHelper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		description = sHelper.addItem(wx.StaticText(self, label=self.panelDescription))
		description.Wrap(self.scaleSize(PANEL_CONTENT_WIDTH))
		names = [providers.displayName(providerId) for providerId in self._providerIds]

		self.defaultServiceCtrl = sHelper.addLabeledControl(
			# Translators: Label of the choice of the AI service that new conversations use.
			_("Default AI ser&vice:"),
			wx.Choice,
			choices=names,
		)
		self.defaultServiceCtrl.SetSelection(self._providerIds.index(defaultProvider()))

		self.accountCtrl = sHelper.addLabeledControl(
			# Translators: Label of the choice of which service's account, program and defaults are shown below.
			_("Account settin&gs for:"),
			wx.Choice,
			choices=names,
		)
		self.accountCtrl.SetSelection(self._providerIds.index(shown))
		self.accountCtrl.Bind(wx.EVT_CHOICE, self.onAccountChoice)

		# A single-line read-only TextCtrl is skipped by Tab in wx, so the status is a read-only
		# multi-line box one line high: wx lets Tab reach read-only multi-line boxes.
		self.statusCtrl = sHelper.addLabeledControl(
			# Translators: Label of the read-only sign-in status field.
			_("Status:"),
			wx.TextCtrl,
			style=STATUS_STYLE,
			size=(self.scaleSize(STATUS_WIDTH), -1),
		)
		# One line high: sets both the minimum and the current size, so the sizer keeps it.
		self.statusCtrl.SetInitialSize(
			(self.scaleSize(STATUS_WIDTH), int(self.statusCtrl.GetCharHeight() * 1.6)),
		)
		# Translators: Status shown before the check finishes.
		self.statusCtrl.SetValue(_("Checking…"))

		# Two rows of buttons, so each row fits NVDA's settings area.
		signInButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Label set by _updateAccountLabels.
		self.signInButton = signInButtons.addButton(self, label=self._signInLabel(shown))
		self.signInButton.Bind(wx.EVT_BUTTON, self.onSignIn)
		# Translators: Button that checks the sign-in status.
		checkButton = signInButtons.addButton(self, label=_("C&heck status"))
		checkButton.Bind(wx.EVT_BUTTON, lambda evt: self.refreshStatus(announce=True))
		sHelper.addItem(signInButtons)
		accountButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: Button that signs out of the service in its program.
		signOutButton = accountButtons.addButton(self, label=_("Sign ou&t"))
		signOutButton.Bind(wx.EVT_BUTTON, self.onSignOut)
		# Translators: Button that copies the sign-in web address, for when the browser did not open.
		self.copyLinkButton = accountButtons.addButton(self, label=_("Cop&y sign-in link"))
		self.copyLinkButton.Bind(wx.EVT_BUTTON, self.onCopyLink)
		# Translators: Button (Claude only) to type the code the sign-in page shows after using the copied link.
		self.enterCodeButton = accountButtons.addButton(self, label=_("Enter sign-in code..."))
		self.enterCodeButton.Bind(wx.EVT_BUTTON, self.onEnterCode)
		sHelper.addItem(accountButtons)

		# The path label sits on its own line; the field stretches next to Browse.
		# The label is created right before the field, so Windows uses it as the field's name.
		self.pathLabel, self.pathCtrl, browseButton = self._labelledPathRow(
			sHelper,
			self._pathLabel(shown),
			# Translators: Button to choose the program file.
			_("&Browse..."),
			self._values[shown]["path"],
		)
		browseButton.Bind(wx.EVT_BUTTON, self.onBrowse)

		# Editable: the list comes from the service (Codex's own model list, Claude Code's aliases),
		# and any other model name can be typed. The label sits above the box so long names do not widen the panel.
		modelLabel = wx.StaticText(
			self,
			# Translators: Label of the default model field.
			label=_("Default &model (blank uses your plan's default):"),
		)
		self.modelCtrl = wx.ComboBox(
			self,
			choices=[],
			style=wx.CB_DROPDOWN,
			size=(self.scaleSize(PATH_MIN_WIDTH), -1),
		)
		modelBlock = wx.BoxSizer(wx.VERTICAL)
		modelBlock.Add(modelLabel)
		modelBlock.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_VERTICAL)
		modelBlock.Add(self.modelCtrl, flag=wx.EXPAND)
		sHelper.addItem(modelBlock, flag=wx.EXPAND)

		self._effortValues = []
		self.effortCtrl = sHelper.addLabeledControl(
			# Translators: Label of the default reasoning effort choice.
			_("Default &reasoning effort:"),
			wx.Choice,
			choices=[],
		)

		# Label above the box, which then uses the full panel width without widening the panel.
		instructionsLabel = wx.StaticText(
			self,
			# Translators: Label of the instructions (system prompt) field.
			label=_("&Instructions for the AI:"),
		)
		self.instructionsCtrl = wx.TextCtrl(
			self,
			style=wx.TE_MULTILINE,
			size=self.scaleSize((INSTRUCTIONS_MIN_WIDTH, 100)),
		)
		self.instructionsCtrl.SetValue(conf["instructions"])
		instructionsBlock = wx.BoxSizer(wx.VERTICAL)
		instructionsBlock.Add(instructionsLabel)
		instructionsBlock.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_VERTICAL)
		instructionsBlock.Add(self.instructionsCtrl, flag=wx.EXPAND)
		sHelper.addItem(instructionsBlock, flag=wx.EXPAND)

		self.speakCheck = sHelper.addItem(
			# Translators: Checkbox to speak answers automatically.
			wx.CheckBox(self, label=_("S&peak answers automatically")),
		)
		self.speakCheck.SetValue(conf["speakAnswers"])
		self.contextCheck = sHelper.addItem(
			# Translators: Checkbox to keep conversation context.
			wx.CheckBox(self, label=_("&Keep conversation context for follow-up questions")),
		)
		self.contextCheck.SetValue(conf["keepContext"])
		self.beepCheck = sHelper.addItem(
			# Translators: Checkbox to play a soft beep every two seconds while waiting for an answer.
			wx.CheckBox(self, label=_("Beep &while waiting for an answer")),
		)
		self.beepCheck.SetValue(conf["beepWhileWaiting"])
		self.projectWritesCheck = sHelper.addItem(
			# Translators: Checkbox that lets the AI create and edit files in a conversation's project folder.
			wx.CheckBox(self, label=_("Allow the AI to create and edit files in the pro&ject folder")),
		)
		self.projectWritesCheck.SetValue(conf["allowProjectWrites"])
		# No letter is left for this one (the panel avoids O, C and A); it is reached with Tab.
		self.announceCheck = sHelper.addItem(
			# Translators: Checkbox: say "Done: <conversation>" or "Failed: ..." when a question finishes while
			# the user works in another application.
			wx.CheckBox(self, label=_("Announce when answers finish while working in other apps")),
		)
		self.announceCheck.SetValue(conf["announceInOtherApps"])

		self.timeoutCtrl = sHelper.addLabeledControl(
			# Translators: Label of the timeout spin control.
			_("A&nswer timeout in seconds:"),
			nvdaControls.SelectOnFocusSpinCtrl,
			min=providerBase.MIN_TIMEOUT,
			max=providerBase.MAX_TIMEOUT,
			initial=conf["timeout"],
		)
		self.dictationTimeoutCtrl = sHelper.addLabeledControl(
			# Translators: Label of the spin control: seconds of no new dictated text before NVDA speaks again.
			_("&Dictation silence timeout in seconds:"),
			nvdaControls.SelectOnFocusSpinCtrl,
			min=dictation.MIN_SILENCE_TIMEOUT,
			max=dictation.MAX_SILENCE_TIMEOUT,
			initial=int(conf["dictationSilenceTimeout"]),
		)

		_conversationsLabel, self.conversationsDirCtrl, chooseFolderButton = self._labelledPathRow(
			sHelper,
			# Translators: Label of the folder where conversations are saved. Blank means the default folder.
			_("Conversations &folder (blank for the default):"),
			# Translators: Button to choose the conversations folder.
			_("Choos&e folder..."),
			conf["conversationsDir"],
		)
		chooseFolderButton.Bind(wx.EVT_BUTTON, self.onChooseConversationsDir)

		self.autoUpdateCheck = sHelper.addItem(
			# Translators: Checkbox to check GitHub for new Ask AI versions once a day.
			wx.CheckBox(self, label=_("Check for updates automatica&lly")),
		)
		self.autoUpdateCheck.SetValue(conf["autoUpdateCheck"])
		updateButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: Button that checks for a newer Ask AI version now.
		updateButton = updateButtons.addButton(self, label=_("Check for &updates now"))
		updateButton.Bind(wx.EVT_BUTTON, self.onCheckForUpdates)
		sHelper.addItem(updateButtons)

		self._loadAccountValues()

	@staticmethod
	def _signInLabel(providerId):
		# Translators: Button that starts sign-in in the browser. {service} is "ChatGPT" or "Claude".
		return _("&Sign in to {service}...").format(service=serviceName(providerId))

	@staticmethod
	def _pathLabel(providerId):
		# Translators: Label of the program path field. {program} is "Codex" or "Claude Code".
		return _("{program} e&xecutable path (blank to detect automatically):").format(program=programName(providerId))

	def _labelledPathRow(self, sHelper, label, buttonLabel, value):
		"""Label on its own line, then a stretching text field and a button. Returns (label, field, button)."""
		labelCtrl = wx.StaticText(self, label=label)
		field = wx.TextCtrl(self, size=(self.scaleSize(PATH_MIN_WIDTH), -1))
		field.SetValue(value)
		button = wx.Button(self, label=buttonLabel)
		row = wx.BoxSizer(wx.HORIZONTAL)
		row.Add(field, proportion=1, flag=wx.ALIGN_CENTER_VERTICAL)
		row.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_HORIZONTAL)
		row.Add(button, flag=wx.ALIGN_CENTER_VERTICAL)
		block = wx.BoxSizer(wx.VERTICAL)
		block.Add(labelCtrl)
		block.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_VERTICAL)
		block.Add(row, flag=wx.EXPAND)
		sHelper.addItem(block, flag=wx.EXPAND)
		return labelCtrl, field, button

	def _onDestroy(self, evt):
		global _activePanel
		if evt.GetEventObject() is self and _activePanel is self:
			_activePanel = None
		evt.Skip()

	def onPanelActivated(self):
		super().onPanelActivated()
		self.refreshStatus(announce=False)

	# Switching the account section between services

	def _effortChoicesFor(self, providerId):
		values = list(providers.get(providerId).defaultEfforts)
		for model in self._models.get(providerId, []):
			for effort in model.efforts:
				if effort not in values:
					values.append(effort)
		effort = self._values[providerId]["effort"]
		if effort and effort not in values:
			values.append(effort)
		return values

	def _loadAccountValues(self):
		"""Show the shown service's path, models and effort, and its labels and buttons."""
		providerId = self._shownProvider
		values = self._values[providerId]
		models = self._models.get(providerId, [])
		self.pathLabel.SetLabel(self._pathLabel(providerId))
		self.signInButton.SetLabel(self._signInLabel(providerId))
		self.pathCtrl.SetValue(values["path"])
		self.modelCtrl.Set([""] + [modelCatalog.comboLabel(model) for model in models])
		configured = modelCatalog.findModel(models, values["model"])
		self.modelCtrl.SetValue(modelCatalog.comboLabel(configured) if configured else values["model"])
		self._effortValues = self._effortChoicesFor(providerId)
		self.effortCtrl.Set([effortLabel(value) for value in self._effortValues])
		effort = values["effort"]
		self.effortCtrl.SetSelection(self._effortValues.index(effort) if effort in self._effortValues else 0)
		self.enterCodeButton.Show(providers.get(providerId).loginAcceptsCode)
		self._updateLoginButtons()
		self.Layout()

	def _storeAccountValues(self):
		providerId = self._shownProvider
		values = self._values[providerId]
		values["path"] = self.pathCtrl.GetValue().strip()
		values["model"] = self._modelSlug()
		selection = self.effortCtrl.GetSelection()
		if 0 <= selection < len(self._effortValues):
			values["effort"] = self._effortValues[selection]

	def _showProvider(self, providerId):
		if providerId == self._shownProvider:
			return
		self._storeAccountValues()
		self._shownProvider = providerId
		self.accountCtrl.SetSelection(self._providerIds.index(providerId))
		self._loadAccountValues()

	def onAccountChoice(self, evt):
		selection = self.accountCtrl.GetSelection()
		if selection < 0:
			return
		self._showProvider(self._providerIds[selection])
		self.refreshStatus(announce=False)

	def _updateLoginButtons(self):
		loginRunning = _loginClient is not None and _loginClient.providerId == self._shownProvider
		self.copyLinkButton.Enable(bool(self._loginUrl) and self._loginUrlProvider == self._shownProvider and loginRunning)
		self.enterCodeButton.Enable(loginRunning and providers.get(self._shownProvider).loginAcceptsCode)

	# Status and account

	def _setStatus(self, generation, text, announce):
		if not self or generation != self._statusGeneration:
			return
		self.statusCtrl.SetValue(text)
		initial = self._initialStatusPending
		self._initialStatusPending = False
		# Also announce when the user is reading the Status field, so its changes are not missed.
		if announce or self.FindFocus() == self.statusCtrl:
			ui.message(text)
		elif initial and self.IsShownOnScreen():
			# The first result after the panel opens, while the user is still on it.
			# Translators: Announced when the Ask AI settings open. {status} is e.g. "Signed in with ChatGPT".
			ui.message(_("Ask AI: {status}").format(status=text))

	def refreshStatus(self, announce=False):
		self._statusGeneration += 1
		generation = self._statusGeneration
		self.statusCtrl.SetValue(_("Checking…"))
		self._updateLoginButtons()
		providerId = self._shownProvider
		client = makeClient(executablePath=self.pathCtrl.GetValue(), providerId=providerId)

		def worker():
			try:
				loginState, _text = client.loginStatus()
			except ProviderError as e:
				log.debugWarning("Ask AI: status check: %s: %s: %s | %s" % (providerId, e.kind, e.message, e.detail))
				status = shortStatusForError(e)
			except Exception:
				log.error("Ask AI: status check failed", exc_info=True)
				# Translators: Status when the check failed.
				status = _("Could not check the status")
			else:
				state.loginStates[providerId] = loginState
				status = loginStatusText(providerId, loginState)
			wx.CallAfter(self._setStatus, generation, status, announce)

		threading.Thread(target=worker, name="askAI.status", daemon=True).start()

	def onSignIn(self, evt):
		global _loginClient
		# Kill any running sign-in before starting a new one.
		cancelLogin()
		providerId = self._shownProvider
		client = makeClient(executablePath=self.pathCtrl.GetValue(), providerId=providerId)
		try:
			client.locate()
		except ProviderError as e:
			showError(friendlyError(e), self)
			self.refreshStatus()
			return
		self._loginUrl = None
		self._loginUrlProvider = None
		self._statusGeneration += 1
		# Translators: Status while the user signs in in the browser.
		self.statusCtrl.SetValue(_("Waiting for you to sign in in your browser"))
		# Translators: Announced before the browser opens for sign-in. {service} is "ChatGPT" or "Claude".
		message = _(
			"Your browser will open. Sign in with your {service} account, then come back to NVDA. "
			"NVDA will tell you when you are signed in."
		).format(service=serviceName(providerId))
		_loginClient = client
		self._updateLoginButtons()
		# Start the browser only after the message, so the focus change does not cut it off.
		speakThen(message, lambda: self._startLogin(client))

	def _startLogin(self, client):
		if _loginClient is not client:
			return  # Cancelled, superseded or shut down while the message was spoken.
		try:
			proc = client.login(onUrl=lambda url: wx.CallAfter(self._onLoginUrl, client, url))
		except ProviderError as e:
			cancelLogin()
			showError(friendlyError(e), self if self else None)
			_refreshPanel()
			return

		def waiter():
			try:
				proc.wait(timeout=LOGIN_TIMEOUT_SECONDS)
				timedOut = False
			except subprocess.TimeoutExpired:
				timedOut = True
			except Exception:
				timedOut = False
			wx.CallAfter(_finishLogin, client, timedOut)

		threading.Thread(target=waiter, name="askAI.login", daemon=True).start()

	def _onLoginUrl(self, client, url):
		if not self or _loginClient is not client:
			return
		self._loginUrl = url
		self._loginUrlProvider = client.providerId
		self._updateLoginButtons()

	def onCopyLink(self, evt):
		if not self._loginUrl:
			return
		if api.copyToClip(self._loginUrl):
			if providers.get(self._loginUrlProvider).loginAcceptsCode:
				# Translators: Announced after copying a sign-in link that ends with a code to type back.
				message = _(
					"Sign-in link copied. Paste it into your browser's address bar. If the page shows a code, "
					"copy it and press Enter sign-in code."
				)
			else:
				# Translators: Announced after copying the sign-in link.
				message = _("Sign-in link copied. Paste it into your browser's address bar.")
			ui.message(message)
		else:
			ui.message(_("Could not copy to the clipboard."))

	def onEnterCode(self, evt):
		client = _loginClient
		if client is None or client.providerId != self._shownProvider:
			return
		with wx.TextEntryDialog(
			self,
			# Translators: Prompt for the code the sign-in web page shows.
			_("Paste the code shown on the sign-in page:"),
			# Translators: Title of the dialog for the sign-in code.
			_("Sign-in code"),
		) as codeDialog:
			if codeDialog.ShowModal() != wx.ID_OK:
				return
			code = codeDialog.GetValue().strip()
		if _loginClient is not client:
			# Translators: Reported when the sign-in ended before the code was entered.
			showError(_("The sign-in has ended. Press Sign in to start again."), self)
			return
		if client.submitLoginCode(code):
			# Translators: Announced after the sign-in code was passed on.
			ui.message(_("Code sent. NVDA will tell you when you are signed in."))
		else:
			# Translators: Error when the sign-in code could not be used.
			showError(_("That code could not be used. Copy the whole code from the sign-in page and try again."), self)

	def onSignOut(self, evt):
		providerId = self._shownProvider
		# Translators: Confirmation before signing out. {service} is "ChatGPT" or "Claude", {program} e.g. "Codex".
		question = _("Sign out of {service} in {program}?").format(service=serviceName(providerId), program=programName(providerId))
		if not askYesNo(question, _("Ask AI"), self):
			return
		cancelLogin()
		self._statusGeneration += 1
		generation = self._statusGeneration
		client = makeClient(executablePath=self.pathCtrl.GetValue(), providerId=providerId)

		def worker():
			try:
				ok, text = client.logout()
				if ok:
					state.loginStates[providerId] = providerBase.STATE_SIGNED_OUT
				# Translators: Status after signing out.
				status = _("Signed out") if ok else (text.splitlines()[0] if text else _("Not signed in"))
			except ProviderError as e:
				log.debugWarning("Ask AI: sign out: %s: %s | %s" % (e.kind, e.message, e.detail))
				status = shortStatusForError(e)
			except Exception:
				log.error("Ask AI: sign out failed", exc_info=True)
				# Translators: Status when signing out failed.
				status = _("Could not sign out")
			wx.CallAfter(self._setStatus, generation, status, True)

		threading.Thread(target=worker, name="askAI.logout", daemon=True).start()

	def onBrowse(self, evt):
		currentPath = providerBase.expandUserPath(self.pathCtrl.GetValue())
		with wx.FileDialog(
			self,
			# Translators: Title of the file chooser for the program. {program} is "Codex" or "Claude Code".
			_("Select the {program} program").format(program=programName(self._shownProvider)),
			defaultDir=os.path.dirname(currentPath),
			# Translators: File type filter in the file chooser.
			wildcard=_("Programs (*.exe;*.cmd)|*.exe;*.cmd|All files (*.*)|*.*"),
			style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
		) as fileDialog:
			if fileDialog.ShowModal() == wx.ID_OK:
				self.pathCtrl.SetValue(fileDialog.GetPath())
				self.refreshStatus(announce=True)

	def onChooseConversationsDir(self, evt):
		current = providerBase.expandUserPath(self.conversationsDirCtrl.GetValue()) or state.conversationsFolder()
		with wx.DirDialog(
			self,
			# Translators: Title of the folder chooser for saved conversations.
			_("Choose where Ask AI saves conversations"),
			defaultPath=current if os.path.isdir(current) else "",
			style=wx.DD_DEFAULT_STYLE,
		) as folderDialog:
			if folderDialog.ShowModal() == wx.ID_OK:
				self.conversationsDirCtrl.SetValue(folderDialog.GetPath())

	def onCheckForUpdates(self, evt):
		from . import updateCheck

		updateCheck.checkForUpdates(self)

	# Saving

	def _modelSlug(self):
		return modelCatalog.slugFromComboText(self.modelCtrl.GetValue(), self._models.get(self._shownProvider, []))

	def isValid(self):
		self._storeAccountValues()
		for providerId in self._providerIds:
			values = self._values[providerId]
			rawPath = values["path"]
			# Only check the path when it was changed, so an unplugged drive does not block saving other settings.
			if rawPath and rawPath != providerSettings(providerId)[0]:
				path = providerBase.expandUserPath(rawPath)
				if not os.path.isfile(path):
					self._showProvider(providerId)
					showError(
						# Translators: Error when a program path does not exist. {program} is e.g. "Codex".
						_("The {program} executable path does not exist: {path}").format(program=programName(providerId), path=path),
						self,
					)
					self.pathCtrl.SetFocus()
					return False
			if not providerBase.isValidModel(values["model"]):
				self._showProvider(providerId)
				showError(
					# Translators: Error when the model name has invalid characters.
					_("The model name may only contain letters, digits, dots, dashes, colons, slashes and brackets."),
					self,
				)
				self.modelCtrl.SetFocus()
				return False
		rawFolder = self.conversationsDirCtrl.GetValue().strip()
		if rawFolder and rawFolder != getConf()["conversationsDir"]:
			folder = providerBase.expandUserPath(rawFolder)
			try:
				os.makedirs(folder, exist_ok=True)
			except OSError as e:
				# Translators: Error when the conversations folder cannot be used. {error} is the reason.
				showError(_("The conversations folder cannot be used: {error}").format(error=e), self)
				self.conversationsDirCtrl.SetFocus()
				return False
		return super().isValid()

	def onSave(self):
		self._storeAccountValues()
		conf = getConf()
		selection = self.defaultServiceCtrl.GetSelection()
		conf["provider"] = self._providerIds[selection] if selection >= 0 else providers.DEFAULT_PROVIDER
		for providerId in self._providerIds:
			keys = PROVIDER_CONFIG_KEYS[providerId]
			values = self._values[providerId]
			conf[keys["path"]] = values["path"]
			conf[keys["model"]] = values["model"]
			conf[keys["effort"]] = values["effort"] or providerBase.DEFAULT_REASONING_EFFORT
		instructions = self.instructionsCtrl.GetValue().strip()
		conf["instructions"] = instructions or providerBase.DEFAULT_INSTRUCTIONS
		conf["speakAnswers"] = self.speakCheck.GetValue()
		conf["keepContext"] = self.contextCheck.GetValue()
		conf["beepWhileWaiting"] = self.beepCheck.GetValue()
		conf["allowProjectWrites"] = self.projectWritesCheck.GetValue()
		conf["announceInOtherApps"] = self.announceCheck.GetValue()
		conf["timeout"] = providerBase.clampTimeout(self.timeoutCtrl.GetValue())
		conf["dictationSilenceTimeout"] = int(dictation.clampSilenceTimeout(self.dictationTimeoutCtrl.GetValue()))
		conf["conversationsDir"] = self.conversationsDirCtrl.GetValue().strip()
		conf["autoUpdateCheck"] = self.autoUpdateCheck.GetValue()


def shortStatusForError(error):
	"""One short sentence for the Status field."""
	providerId = getattr(error, "provider", "") or defaultProvider()
	program = programName(providerId)
	if error.kind == providerBase.KIND_NOT_INSTALLED:
		if error.reason == providerBase.REASON_NODE_MISSING:
			# Translators: Status when Node.js is missing.
			return _("Node.js not found")
		if error.reason == providerBase.REASON_CUSTOM_PATH:
			# Translators: Status when the configured path is wrong. {program} is e.g. "Codex".
			return _("{program} executable path not found").format(program=program)
		# Translators: Status when the program cannot be found. {program} is e.g. "Codex".
		return _("{program} not found").format(program=program)
	if error.kind == providerBase.KIND_NETWORK:
		# Translators: Status when the service cannot be reached. {service} is "ChatGPT" or "Claude".
		return _("Could not reach {service}").format(service=serviceName(providerId))
	if error.kind == providerBase.KIND_TIMEOUT:
		# Translators: Status when the check took too long.
		return _("The status check timed out")
	if error.kind == providerBase.KIND_CONFIG and error.reason == providerBase.REASON_OUTDATED:
		# Translators: Status when the program is too old. {program} is e.g. "Claude Code".
		return _("{program} needs an update").format(program=program)
	# Translators: Status when the check failed.
	return _("Could not check the status")
