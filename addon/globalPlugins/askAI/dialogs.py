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

from . import attachments, conversations, dictation, exporters, folderSnapshot, history, migration, projectSafety, providers, search
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
# speakThen's wait when speech is not in talk mode (no synthesizer callback will come).
NON_TALK_DELAY_MS = 400
# How long to wait for the user to finish signing in in the browser (Claude may need a pasted code).
LOGIN_TIMEOUT_SECONDS = 300
# After moving focus, announcements wait until NVDA's focus is on the target control (checked every
# FOCUS_POLL_MS, at most FOCUS_WAIT_MS), so NVDA's own focus announcement does not swallow them.
FOCUS_POLL_MS = 50
FOCUS_WAIT_MS = 500
# Kept for older callers.
ANNOUNCE_AFTER_FOCUS_MS = FOCUS_POLL_MS
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
# The settings check the sign-in status this long after the shown service last changed.
STATUS_DEBOUNCE_MS = 400


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


def announceWhenFocused(control, message):
	"""Speak message once NVDA's focus object is control (polled every 50 ms, at most 500 ms; then anyway)."""
	waited = [0]

	def check():
		ready = True
		try:
			if control:
				import api

				focus = api.getFocusObject()
				ready = focus is not None and focus.windowHandle == control.GetHandle()
		except Exception:
			ready = True
		if ready or waited[0] >= FOCUS_WAIT_MS:
			ui.message(message)
			return
		waited[0] += FOCUS_POLL_MS
		wx.CallLater(FOCUS_POLL_MS, check)

	wx.CallLater(FOCUS_POLL_MS, check)


class RequestRefused(Exception):
	"""A question that cannot be sent as it is; message is a translated explanation."""

	def __init__(self, message):
		super().__init__(message)
		self.message = message


def checkProjectFolder(path):
	"""(folder, "") when path may be a project folder, else (None, reason). Looks at the text (and the
	drive type) before anything touches the folder, so a network path is never contacted.
	"""
	if not path:
		return None, projectSafety.REASON_EMPTY
	return projectSafety.checkResolved(path)


def projectRefusalText(path, reason):
	"""Why a folder cannot be a project folder, in plain words."""
	texts = {
		# Translators: Why a folder cannot be a project folder.
		projectSafety.REASON_NOT_ABSOLUTE: _("it is not a full path on a local drive"),
		# Translators: Why a folder cannot be a project folder.
		projectSafety.REASON_NETWORK: _("network and device paths are not allowed"),
		# Translators: Why a folder cannot be a project folder.
		projectSafety.REASON_DRIVE_ROOT: _("a whole drive cannot be a project folder"),
		# Translators: Why a folder cannot be a project folder.
		projectSafety.REASON_NOT_FIXED: _("it is not on a fixed local drive (removable, network and CD drives are not allowed)"),
		# Translators: Why a folder cannot be a project folder.
		projectSafety.REASON_SYSTEM: _("it is a Windows, program or startup folder"),
		# Translators: Why a folder cannot be a project folder.
		projectSafety.REASON_PROFILE: _("your whole user folder cannot be a project folder"),
		# Translators: Why a folder cannot be a project folder.
		projectSafety.REASON_APPDATA: _("it is inside the AppData folder, where programs keep their settings"),
		# Translators: Why a folder cannot be a project folder.
		projectSafety.REASON_ADDON_DATA: _("it is inside Ask AI's own data folder"),
	}
	# Translators: Error when a folder cannot be used. {folder} is the path, {reason} why.
	return _("{folder} cannot be a project folder: {reason}. Choose a folder of your own, for example in Documents.").format(
		folder=path,
		reason=texts.get(reason, reason),
	)


def dropUnsafeProjectFolder(conversation):
	"""Remove a project folder that may not be used (for example from an edited or synced file).
	Returns a spoken explanation, or "".
	"""
	if not conversation.projectFolder:
		return ""
	_folder, reason = checkProjectFolder(conversation.projectFolder)
	if not reason:
		return ""
	path = conversation.projectFolder
	conversation.projectFolder = ""
	log.warning("Ask AI: removed an unsafe project folder from a conversation: %s (%s)" % (path, reason))
	# Translators: Announced when a conversation's project folder was refused. {text} explains why.
	return _("The project folder was removed. {text}").format(text=projectRefusalText(path, reason))


def existingProjectFolder(conversation):
	"""The conversation's project folder if it is allowed and exists, else "". Checked before touching it."""
	folder, reason = checkProjectFolder(conversation.projectFolder)
	if reason:
		return ""
	return folder if os.path.isdir(folder) else ""


def openProjectFolder(path, parent=None):
	folder, reason = checkProjectFolder(path)
	if reason:
		if path:
			showError(projectRefusalText(path, reason), parent)
		return
	if not os.path.isdir(folder):
		# Translators: Error when the project folder was moved or deleted. {path} is the folder.
		showError(_("The project folder no longer exists: {path}. Choose another folder or clear it.").format(path=folder), parent)
		return
	try:
		os.startfile(folder)
	except Exception as e:
		log.error("Ask AI: could not open the project folder", exc_info=True)
		# Translators: Error when Explorer could not open the folder. {error} is the reason.
		showError(_("Could not open the folder: {error}").format(error=e), parent)


def projectGrants():
	try:
		return list(getConf()["projectGrants"])
	except Exception:
		return []


def writeConsent(conversation, parent=None):
	"""Whether the AI may write in the conversation's project folder for this request. The first time a folder
	is used with writing allowed, the user is asked once; Yes is kept in askAI.projectGrants (NVDA
	configuration, never the conversation file). Without consent the request runs read-only.
	"""
	if not conversation.projectFolder or not getConf()["allowProjectWrites"]:
		return False
	folder, reason = checkProjectFolder(conversation.projectFolder)
	if reason:
		return False
	grants = projectGrants()
	if projectSafety.hasGrant(grants, folder):
		return True
	# Translators: Asked the first time the AI could write in a project folder. {folder} is the folder.
	question = _("Allow the AI to create and change files in {folder}?").format(folder=folder)
	# Translators: Title of the question about writing in the project folder.
	if askYesNo(question, _("Project folder"), parent):
		getConf()["projectGrants"] = projectSafety.addGrant(grants, folder)
		return True
	return False


def requestRefusal(conversation):
	"""A reason why the question cannot be sent as it is, or ""."""
	if conversation.projectFolder:
		_folder, reason = checkProjectFolder(conversation.projectFolder)
		if reason:
			return projectRefusalText(conversation.projectFolder, reason)
	providerId = conversation.provider if providers.isKnown(conversation.provider) else providers.DEFAULT_PROVIDER
	if not providers.get(providerId).readsOtherFiles:
		others = [item.name for item in state.pendingAttachments if item.kind == attachments.KIND_FILE]
		if others:
			# Translators: Error when files other than text and images are attached for ChatGPT. {files} lists them.
			return _(
				"ChatGPT can only receive text files and images; Claude can read other files. "
				"Remove {files}, or switch this conversation to Claude in Conversation options."
			).format(files=", ".join(others))
	return ""


def clearScreenContext():
	"""Screen text and the screenshot choice are for one question only and never carried along silently."""
	state.pendingContext = None
	state.includeScreenshot = False


def contextSummaryTemplates():
	return {
		# Translators: Summary of attached screen text. {app} e.g. "Word", {count} e.g. "1,240".
		"selection": _("Selected text from {app}, {count} characters"),
		# Translators: Summary of attached screen text that was shortened. {count} is "20,000".
		"selectionCut": _("Selected text from {app}, cut to {count} characters"),
		# Translators: Summary of attached screen text. {role} e.g. "document", {app} e.g. "Edge".
		"object": _("Text of the current object ({role}) from {app}, {count} characters"),
		# Translators: Summary of attached screen text that was shortened.
		"objectCut": _("Text of the current object ({role}) from {app}, cut to {count} characters"),
		# Translators: Summary of attached screen text without a known object type.
		"objectNoRole": _("Text of the current object from {app}, {count} characters"),
		# Translators: Summary of attached screen text without a known object type, shortened.
		"objectNoRoleCut": _("Text of the current object from {app}, cut to {count} characters"),
		# Translators: Used when the application is unknown, as in "Selected text from the screen".
		"theScreen": _("the screen"),
	}


def contextSummary(context):
	return context.summary(contextSummaryTemplates())


def screenshotLabel(window):
	if window:
		# Translators: Checkbox to send a picture of the window. {window} is the window title.
		return _("Incl&ude a screenshot of {window} (everything visible in it)").format(window=window)
	# Translators: Checkbox to send a picture of the window, when its title is unknown.
	return _("Incl&ude a screenshot of the window (everything visible in it)")


def localClock(timestamp):
	"""Time in the Windows locale (NVDA's winKernel.GetTimeFormatEx, as NVDA's own time command), with the
	date when it is not today; the 12-hour fallback elsewhere.
	"""
	import datetime

	try:
		import winKernel

		moment = datetime.datetime.fromtimestamp(timestamp)
		text = winKernel.GetTimeFormatEx(winKernel.LOCALE_NAME_USER_DEFAULT, winKernel.TIME_NOSECONDS, moment, None)
		if moment.date() != datetime.date.today():
			date = winKernel.GetDateFormatEx(winKernel.LOCALE_NAME_USER_DEFAULT, getattr(winKernel, "DATE_SHORTDATE", 1), moment, None)
			text = "%s %s" % (date, text)
		if text:
			return text
	except Exception:
		pass
	return statusText.formatClock(timestamp)


def offerToRemoveOldAddon(parent=None):
	"""The first time Ask AI is opened while Ask GPT 1.x is still installed and enabled (both would take
	NVDA+Alt+G), ask whether to remove it. The answer is recorded only after an explicit Yes or No;
	Escape asks again next time.
	"""
	conf = getConf()
	if conf["askedToRemoveAskGPT"]:
		return
	oldAddon = findOldAddon()
	if oldAddon is None:
		return
	answer = askYesNoOrLater(
		# Translators: Asked once when the old Ask GPT add-on is still installed next to Ask AI.
		_("The old Ask GPT add-on is still installed. Remove it now? (Restart NVDA afterwards.)"),
		# Translators: Title of the question about removing the old add-on.
		_("Ask AI"),
		parent,
	)
	if answer is None:
		return
	conf["askedToRemoveAskGPT"] = True
	if answer:
		removeOldAddon(oldAddon)


def findOldAddon():
	try:
		return migration.findOldAddon(addonHandler.getAvailableAddons())
	except Exception:
		log.debugWarning("Ask AI: could not list add-ons", exc_info=True)
		return None


def removeOldAddon(oldAddon):
	try:
		# Addon.requestRemove() (NVDA 2024.1 to 2026.x) marks it for removal at the next restart.
		oldAddon.requestRemove()
	except Exception:
		log.error("Ask AI: could not remove the Ask GPT add-on", exc_info=True)
		# Translators: Error when the old add-on could not be marked for removal.
		showError(_("Ask GPT could not be removed. Remove it in NVDA menu, Tools, Add-on Store, Installed add-ons."))
		return False
	try:
		from gui import addonGui

		wx.CallAfter(addonGui.promptUserForRestart)
	except Exception:
		# Translators: Announced after the old add-on was marked for removal.
		ui.message(_("Ask GPT will be removed when NVDA restarts."))
	return True


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


def makeClient(executablePath=None, conversation=None, providerId=None, allowWrites=None):
	"""A provider instance for the conversation's service (or providerId, or the default service).
	allowWrites: whether this request may write in the project folder (see writeConsent); None means no.
	"""
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
		allowProjectWrites=bool(allowWrites) and conf["allowProjectWrites"],
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


def fileChangesText(created, changed, many=False):
	"""One line such as "Created: a.docx, b.pdf. Changed: x.txt", or "" when nothing changed.
	many: the folder had too many files to compare them all.
	"""
	parts = []
	if many:
		# Translators: Announced after an answer when the project folder has too many files to list changes.
		parts.append(_("Many files changed or the folder is too large to list the changes"))
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
	wx.CallLater(fallbackDelayMs(message), callback)


def fallbackDelayMs(message):
	"""How long to wait for speakThen's callback. Only talk mode passes the message to the synthesizer
	(beeps, on-demand and off do not), so in the other modes the callback never fires and waiting for the
	estimated speech time would just delay the action by several seconds.
	"""
	try:
		import speech

		if speech.getState().speechMode != speech.SpeechMode.talk:
			return NON_TALK_DELAY_MS
	except Exception:
		pass
	return estimatedSpeechMs(message)


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


def buildRequestPrompt(question, conversation, attachmentSection="", allowWrites=False):
	"""The prompt for a question: instructions (with the service's project folder rules), earlier
	conversation, attached screen text and files (fenced, as data), then the question.
	"""
	conf = getConf()
	history = conversation.historyItems if conf["keepContext"] else []
	projectInstructions = ""
	if conversation.projectFolder:
		projectInstructions = providers.get(conversation.provider).projectInstructions(bool(allowWrites))
	return providerBase.buildPrompt(
		question,
		history,
		conf["instructions"],
		projectInstructions=projectInstructions,
		attachmentSection=attachmentSection,
	)


def startRequest(question, conversation, allowWrites=False):
	"""Send question with whatever is attached (screen context, screenshot, files). Main thread.
	A question over attachments.LONG_QUESTION_CHARS is sent as an attached file, question.txt.
	The attachments apply to this question only and are cleared afterwards.
	@raise RequestRefused: unsafe project folder, or files the service cannot read.
	"""
	refusal = requestRefusal(conversation)
	if refusal:
		raise RequestRefused(refusal)
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
	prompt = buildRequestPrompt(sentQuestion, conversation, section, allowWrites)
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
		makeClient(conversation=conversation, allowWrites=allowWrites),
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
			after = folderSnapshot.snapshot(pending.projectFolder)
			created, changed = folderSnapshot.compare(before, after)
			if folderSnapshot.isTruncated(before, after):
				pending.manyChanges = True
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
		# Translators: Status report while an answer is coming. {service} e.g. "ChatGPT", {elapsed} e.g.
		# "40 seconds", {title} the conversation title.
		"answering": _("{service}, working for {elapsed}. Conversation: {title}."),
		# Translators: Status report right after a question was sent.
		"answeringJustStarted": _("{service}, just started. Conversation: {title}."),
		# Translators: Status report while voice typing keeps NVDA quiet.
		"dictating": _("Dictating."),
		# Translators: Status report while a sign-in waits for the browser.
		"signingIn": _("Waiting for sign-in."),
		# Translators: Status report when the current service is signed out. {service} is "ChatGPT" or "Claude".
		"notSignedIn": _("Not signed in to {service}."),
		# Translators: Status report when nothing is running. {time} is a time such as "15:05".
		"idleWithAnswer": _("Idle. Last answer from {service} at {time} in {title}."),
		# Translators: Status report when nothing is running and there is no answer yet.
		"idle": _("Idle."),
		# Translators: A duration in seconds, e.g. "40 seconds".
		"seconds": _("{seconds} seconds"),
		# Translators: A duration of one second.
		"oneSecond": _("1 second"),
		# Translators: A duration, e.g. "2 minutes 5 seconds".
		"minutes": _("{minutes} minutes {seconds} seconds"),
		# Translators: Spoken before an answer that arrives while the user works in another app.
		"done": _("Ask AI answer, {title}:"),
		# Translators: Spoken when a question fails while the user works in another app. {reason} explains why.
		"failed": _("Failed: {title}. {reason}"),
		# Translators: As "failed", for a title that already ends with "?" or "!".
		"failedQuestion": _("Failed: {title} {reason}"),
		"untitled": _("Untitled conversation"),
		# Translators: One attached file, in the status report.
		"oneFile": _("1 file"),
		# Translators: Several attached files, in the status report. {count} is a number.
		"files": _("{count} files"),
		# Translators: Status report part. {app} e.g. "Word", {files} e.g. "2 files".
		"attachedBoth": _("Context from {app} and {files} attached to the next question."),
		# Translators: Status report part. {app} e.g. "Word".
		"attachedContext": _("Context from {app} attached to the next question."),
		# Translators: Status report part. {files} e.g. "2 files".
		"attachedFiles": _("{files} attached to the next question."),
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
		changes = fileChangesText(created, changed, getattr(pending, "manyChanges", False))
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
	context = state.pendingContext
	snapshot = statusText.StatusSnapshot(
		answering=answering,
		dictating=_activeDictation is not None,
		signingIn=_loginClient is not None,
		service=serviceName(providerId),
		signedOut=state.loginStates.get(providerId) == providerBase.STATE_SIGNED_OUT,
		lastAnswer=lastAnswer,
		contextApp=(context.appName or context.windowName) if context is not None else "",
		attachedFiles=len(state.pendingAttachments),
	)
	return statusText.statusText(snapshot, now, statusTemplates(), clock=localClock)


def switchConversation(conversation):
	"""Make conversation current (main thread). The running request keeps its own conversation.
	The Question text is kept as the old conversation's draft, and the new one's draft is restored.
	"""
	dialog = AskAIDialog.getInstance()
	if state.current is not None and state.current is not conversation:
		if dialog is not None:
			dialog.saveDraft(silent=True)
		state.save(state.current)
	# Screen text and the screenshot choice belonged to the old conversation's next question.
	clearScreenContext()
	state.current = conversation
	restored = False
	if dialog is not None:
		dialog.questionCtrl.Clear()
		restored = dialog.loadConversation(restoreDraft=True)
		dialog.updatePendingInput()
	return restored


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
			if pending.plan is not None:
				attachments.RequestFiles.remove(pending.plan.directory)
		cancelLogin()
		if state.current is not None:
			state.save(state.current)
		closeAskDialog()


class YesNoDialog(DpiScalingHelperMixinWithoutInit, wx.Dialog):
	"""Yes / No question where Escape means "not now" (returns None), for questions that must only be
	recorded after an explicit answer.
	"""

	def __init__(self, parent, message, title):
		super().__init__(parent, title=title)
		sizer = wx.BoxSizer(wx.VERTICAL)
		text = wx.StaticText(self, label=message)
		text.Wrap(self.scaleSize(420))
		sizer.Add(text, flag=wx.ALL, border=guiHelper.BORDER_FOR_DIALOGS)
		buttons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: Button in a Yes/No question.
		yes = buttons.addButton(self, id=wx.ID_YES, label=_("&Yes"))
		# Translators: Button in a Yes/No question.
		buttons.addButton(self, id=wx.ID_NO, label=_("&No"))
		sizer.Add(buttons.sizer, flag=wx.ALL | wx.ALIGN_RIGHT, border=guiHelper.BORDER_FOR_DIALOGS)
		self.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(evt.GetId()))
		self.SetEscapeId(wx.ID_CANCEL)
		self.SetSizerAndFit(sizer)
		self.CentreOnScreen()
		yes.SetDefault()
		yes.SetFocus()


def askYesNoOrLater(message, title, parent=None):
	"""True for Yes, False for No, None when dismissed with Escape. Main thread."""
	gui.mainFrame.prePopup()
	try:
		dialog = YesNoDialog(parent or gui.mainFrame, message, title)
		dialog.Raise()
		result = dialog.ShowModal()
		dialog.Destroy()
	finally:
		gui.mainFrame.postPopup()
	if result == wx.ID_YES:
		return True
	if result == wx.ID_NO:
		return False
	return None


def effortName(effort):
	"""Short effort name for the options summary ("medium effort")."""
	names = {
		# Translators: Reasoning effort in a sentence such as "Claude, Opus, medium effort".
		"minimal": _("minimal"),
		# Translators: Reasoning effort in a sentence such as "Claude, Opus, low effort".
		"low": _("low"),
		# Translators: Reasoning effort in a sentence such as "Claude, Opus, medium effort".
		"medium": _("medium"),
		# Translators: Reasoning effort in a sentence such as "Claude, Opus, high effort".
		"high": _("high"),
		# Translators: Reasoning effort in a sentence such as "Claude, Opus, extra high effort".
		"xhigh": _("extra high"),
		# Translators: Reasoning effort in a sentence such as "Claude, Opus, maximum effort".
		"max": _("maximum"),
		# Translators: Reasoning effort in a sentence such as "Claude, Opus, ultra effort".
		"ultra": _("ultra"),
	}
	return names.get(effort, effort)


def optionsSummary(conversation):
	""""Claude, Opus, medium effort, no project folder." for the options just applied."""
	providerId = conversation.provider if providers.isKnown(conversation.provider) else defaultProvider()
	model = modelCatalog.findModel(state.modelsFor(providerId), conversation.model) if conversation.model else None
	# Translators: The model in the options summary when the service picks it.
	modelText = model.displayName if model else (conversation.model or _("default model"))
	if conversation.projectFolder:
		# Translators: Part of the options summary. {folder} is the project folder.
		project = _("project folder {folder}").format(folder=conversation.projectFolder)
	else:
		# Translators: Part of the options summary when the conversation has no project folder.
		project = _("no project folder")
	# Translators: Summary after Conversation options, e.g. "Claude, Opus, medium effort, no project folder."
	return _("{service}, {model}, {effort} effort, {project}.").format(
		service=serviceName(providerId),
		model=modelText,
		effort=effortName(conversation.reasoningEffort or providerSettings(providerId)[2]),
		project=project,
	)


class ConversationOptionsDialog(DpiScalingHelperMixinWithoutInit, wx.Dialog):
	"""AI service, model, reasoning effort and project folder of one conversation. Nothing changes until OK.
	Mnemonics: S M E J F X L (OK and Cancel are Enter and Escape).
	"""

	FOCUS_SERVICE = "service"
	FOCUS_MODEL = "model"
	FOCUS_EFFORT = "effort"
	FOCUS_PROJECT = "project"

	def __init__(self, parent, conversation, focus=None):
		# Translators: Title of the conversation options dialog.
		super().__init__(parent, title=_("Conversation options"))
		self.conversation = conversation
		self._providerIds = providers.ids()
		self._provider = conversation.provider if providers.isKnown(conversation.provider) else defaultProvider()
		# Per service: (model, effort), from this conversation, else the service's defaults.
		self._choices = {}
		for providerId in self._providerIds:
			remembered = conversation.settingsFor(providerId)
			if providerId == self._provider:
				remembered = (conversation.model, conversation.reasoningEffort)
			if remembered is None:
				_path, model, effort = providerSettings(providerId)
				remembered = (model, effort)
			self._choices[providerId] = list(remembered)
		self._projectFolder = conversation.projectFolder
		self._modelSlugs = []
		self._effortValues = []
		width = self.scaleSize(420)
		mainSizer = wx.BoxSizer(wx.VERTICAL)
		sHelper = guiHelper.BoxSizerHelper(self, orientation=wx.VERTICAL)
		self.serviceChoice = sHelper.addLabeledControl(
			# Translators: Label of the choice of AI service (ChatGPT or Claude) for this conversation.
			_("AI &service:"),
			wx.Choice,
			choices=[providers.displayName(pid) for pid in self._providerIds],
		)
		self.serviceChoice.SetSelection(self._providerIds.index(self._provider))
		self.serviceChoice.Bind(wx.EVT_CHOICE, self.onServiceChoice)
		self.modelChoice = sHelper.addLabeledControl(
			# Translators: Label of the model choice for this conversation.
			_("&Model:"),
			wx.Choice,
			choices=[],
		)
		self.modelChoice.Bind(wx.EVT_CHOICE, self.onModelChoice)
		self.effortChoice = sHelper.addLabeledControl(
			# Translators: Label of the reasoning effort choice for this conversation.
			_("Reasoning &effort:"),
			wx.Choice,
			choices=[],
		)
		self.effortChoice.Bind(wx.EVT_CHOICE, self.onEffortChoice)
		self.projectCtrl = sHelper.addLabeledControl(
			# Translators: Label of the read-only field showing this conversation's project folder.
			_("Pro&ject folder:"),
			wx.TextCtrl,
			style=STATUS_STYLE,
			size=(width, -1),
		)
		self.projectCtrl.SetInitialSize((width, int(self.projectCtrl.GetCharHeight() * 1.6)))
		projectButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: Button to choose the project folder where the AI may read and save files.
		self.chooseProjectButton = projectButtons.addButton(self, label=_("Choose project &folder..."))
		self.chooseProjectButton.Bind(wx.EVT_BUTTON, self.onChooseProject)
		# Translators: Button that opens the project folder in Windows Explorer.
		self.openProjectButton = projectButtons.addButton(self, label=_("Open folder in E&xplorer"))
		self.openProjectButton.Bind(wx.EVT_BUTTON, self.onOpenProject)
		# Translators: Button that removes the project folder from this conversation.
		self.clearProjectButton = projectButtons.addButton(self, label=_("C&lear project folder"))
		self.clearProjectButton.Bind(wx.EVT_BUTTON, self.onClearProject)
		sHelper.addItem(projectButtons)
		sHelper.addDialogDismissButtons(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), separated=True)
		mainSizer.Add(sHelper.sizer, border=guiHelper.BORDER_FOR_DIALOGS, flag=wx.ALL)
		self.SetSizerAndFit(mainSizer)
		self.CentreOnScreen()
		self._fillModels()
		self._updateProject()
		target = {
			self.FOCUS_MODEL: self.modelChoice,
			self.FOCUS_EFFORT: self.effortChoice,
			self.FOCUS_PROJECT: self.projectCtrl,
		}.get(focus, self.serviceChoice)
		target.SetFocus()

	def _models(self):
		return state.modelsFor(self._provider)

	def _fillModels(self):
		model, effort = self._choices[self._provider]
		providerClass = providers.get(self._provider)
		_path, defaultModel, _defaultEffort = providerSettings(self._provider)
		models = modelCatalog.modelChoices(self._models(), extraSlugs=[model, defaultModel], efforts=providerClass.defaultEfforts)
		self._modelSlugs = [""] + [item.slug for item in models]
		self.modelChoice.Set([defaultModelLabel()] + [item.displayName for item in models])
		self.modelChoice.SetSelection(self._modelSlugs.index(model) if model in self._modelSlugs else 0)
		self._fillEfforts()

	def _fillEfforts(self):
		model, wanted = self._choices[self._provider]
		fallback = providers.get(self._provider).defaultEfforts
		efforts = modelCatalog.effortsFor(self._models(), model, fallback)
		effort = modelCatalog.pickEffort(self._models(), model, wanted or providerSettings(self._provider)[2], fallback)
		self._choices[self._provider][1] = effort
		self._effortValues = list(efforts)
		self.effortChoice.Set([effortLabel(value) for value in efforts])
		self.effortChoice.SetSelection(self._effortValues.index(effort) if effort in self._effortValues else 0)

	def onServiceChoice(self, evt):
		selection = self.serviceChoice.GetSelection()
		if selection >= 0:
			self._provider = self._providerIds[selection]
			self._fillModels()

	def onModelChoice(self, evt):
		selection = self.modelChoice.GetSelection()
		if selection >= 0:
			self._choices[self._provider][0] = self._modelSlugs[selection]
			self._fillEfforts()

	def onEffortChoice(self, evt):
		selection = self.effortChoice.GetSelection()
		if selection >= 0:
			self._choices[self._provider][1] = self._effortValues[selection]

	def _updateProject(self):
		# Translators: Shown in the project folder field when no folder is set.
		self.projectCtrl.SetValue(self._projectFolder or _("None"))
		self.openProjectButton.Enable(bool(self._projectFolder))
		self.clearProjectButton.Enable(bool(self._projectFolder))

	def onChooseProject(self, evt):
		current, _reason = checkProjectFolder(self._projectFolder)
		start = current if current and os.path.isdir(current) else documentsFolder()
		with wx.DirDialog(
			self,
			# Translators: Title of the folder chooser for the project folder.
			_("Choose the project folder for this conversation"),
			defaultPath=start if os.path.isdir(start) else "",
			style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST,
		) as folderDialog:
			if folderDialog.ShowModal() != wx.ID_OK:
				return
			chosen = folderDialog.GetPath()
		folder, reason = checkProjectFolder(chosen)
		if reason:
			showError(projectRefusalText(chosen, reason), self)
			return
		self._projectFolder = folder
		self._updateProject()
		name = os.path.basename(os.path.normpath(folder)) or folder
		# Translators: Announced after choosing the project folder. {name} is the folder name.
		announceWhenFocused(self.chooseProjectButton, _("Project folder: {name}").format(name=name))

	def onOpenProject(self, evt):
		openProjectFolder(self._projectFolder, self)

	def onClearProject(self, evt):
		self._projectFolder = ""
		self._updateProject()
		self.chooseProjectButton.SetFocus()
		# Translators: Announced after removing the project folder.
		announceWhenFocused(self.chooseProjectButton, _("Project folder cleared"))

	def apply(self):
		"""Write the choices into the conversation (OK). Returns the spoken summary."""
		conversation = self.conversation
		for providerId, (model, effort) in self._choices.items():
			conversation.serviceSettings[providerId] = {"model": model, "effort": effort}
		conversation.provider = self._provider
		conversation.model, conversation.reasoningEffort = self._choices[self._provider]
		conversation.projectFolder = self._projectFolder
		conversation.touch()
		state.save(conversation)
		return optionsSummary(conversation)


class AskAIDialog(DpiScalingHelperMixinWithoutInit, wx.Dialog):
	"""Non-modal dialog for asking questions. Only one instance exists at a time.
	Mnemonics: Q A/C D T I U V O W H P R N (Cancel shares the Ask button). Hidden accelerators open
	Conversation options on a field: Alt+S service, Alt+M model, Alt+E effort, Alt+J project folder.
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

		# What goes with the next question (screen text and files); hidden while nothing is attached.
		self.attachedLabel = wx.StaticText(
			self,
			# Translators: Label of the list of screen text and files attached to the next question.
			label=_("Attached to next ques&tion:"),
		)
		self.attachedList = wx.ListBox(self, size=(width - self.scaleSize(140), self.scaleSize(50)))
		self.attachedList.Bind(wx.EVT_KEY_DOWN, self.onAttachedListKey)
		self.attachedList.Bind(wx.EVT_LISTBOX_DCLICK, lambda evt: self.reviewAttached())
		self.removeAttachedButton = wx.Button(
			self,
			# Translators: Button that removes the selected item from what is attached to the next question.
			label=_("Remove &item"),
		)
		self.removeAttachedButton.Bind(wx.EVT_BUTTON, lambda evt: self.removeAttached())
		self.screenshotCheck = wx.CheckBox(self, label=screenshotLabel(""))
		self.screenshotCheck.Bind(wx.EVT_CHECKBOX, self.onScreenshotCheck)
		attachedRow = wx.BoxSizer(wx.HORIZONTAL)
		attachedRow.Add(self.attachedList, proportion=1)
		attachedRow.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_HORIZONTAL)
		attachedRow.Add(self.removeAttachedButton, flag=wx.ALIGN_CENTER_VERTICAL)
		self.attachedBlock = wx.BoxSizer(wx.VERTICAL)
		self.attachedBlock.Add(self.attachedLabel)
		self.attachedBlock.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_VERTICAL)
		self.attachedBlock.Add(attachedRow, flag=wx.EXPAND)
		self.attachedBlock.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_VERTICAL)
		self.attachedBlock.Add(self.screenshotCheck)
		sHelper.addItem(self.attachedBlock, flag=wx.EXPAND)

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
		# Translators: Button that attaches files (documents, images) to the next question.
		self.attachButton = answerButtons.addButton(self, label=_("Attac&h files..."))
		self.attachButton.Bind(wx.EVT_BUTTON, self.onAttachFiles)
		# Translators: Button that opens the service, model, effort and project folder of this conversation.
		self.optionsButton = answerButtons.addButton(self, label=_("Conversation o&ptions..."))
		self.optionsButton.Bind(wx.EVT_BUTTON, lambda evt: self.openOptions())
		sHelper.addItem(answerButtons)

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

		# Alt+S, Alt+M, Alt+E and Alt+J open Conversation options on that field (no visible duplicate letters).
		self._acceleratorFocus = {}
		entries = []
		for letter, focus in (
			("S", ConversationOptionsDialog.FOCUS_SERVICE),
			("M", ConversationOptionsDialog.FOCUS_MODEL),
			("E", ConversationOptionsDialog.FOCUS_EFFORT),
			("J", ConversationOptionsDialog.FOCUS_PROJECT),
		):
			commandId = wx.NewIdRef()
			self._acceleratorFocus[int(commandId)] = focus
			entries.append(wx.AcceleratorEntry(wx.ACCEL_ALT, ord(letter), commandId))
			self.Bind(wx.EVT_MENU, self.onOptionsAccelerator, id=commandId)
		self.SetAcceleratorTable(wx.AcceleratorTable(entries))

		self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)
		self.Bind(wx.EVT_CLOSE, self.onClose)
		self.Bind(wx.EVT_TIMER, self.onBeepTimer, self.beepTimer)
		self.Bind(wx.EVT_TIMER, self.onDictationTimer, self.dictationTimer)
		state.reloadModels()
		self.loadConversation(restoreDraft=True, announce=True)
		self.updatePendingInput()
		mainSizer.Fit(self)
		self.CentreOnScreen()
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

	def loadConversation(self, restoreDraft=False, announce=False):
		"""Show the current conversation. With restoreDraft (opening or continuing it), its saved draft goes
		into an empty Question box. Returns True when a draft was restored (announced if announce).
		An unsafe project folder from the file is removed (and said).
		"""
		conversation = self.conversation
		self._matchStart = None
		self._recall.reset()
		refused = dropUnsafeProjectFolder(conversation)
		self._updateTitle()
		self._shownEntries = []
		self._refreshConversationBox(rebuild=True)
		if self._lastAnswerPos is not None:
			# Opening or switching lands on the beginning of the newest answer.
			self.conversationCtrl.SetInsertionPoint(self._lastAnswerPos)
			self.conversationCtrl.ShowPosition(self._lastAnswerPos)
		restored = False
		if restoreDraft and conversation.draft and not self.questionCtrl.GetValue().strip():
			self.questionCtrl.SetValue(conversation.draft)
			self.questionCtrl.SetInsertionPointEnd()
			restored = True
		messages = []
		if refused:
			messages.append(refused)
		if restored and announce:
			# Translators: Announced when a conversation opens with its unsent question.
			messages.append(_("Draft restored"))
		if messages:
			announceWhenFocused(self.questionCtrl, " ".join(messages))
		return restored

	def _updateTitle(self):
		title = self.conversation.title
		service = serviceName(self.providerId)
		if title:
			# Translators: Title of the Ask AI dialog, e.g. "Ask AI – Trip to Paris – Claude".
			self.SetTitle(_("Ask AI – {title} – {service}").format(title=title, service=service))
		else:
			# Translators: Title of the Ask AI dialog for a new conversation, e.g. "Ask AI – Claude".
			self.SetTitle(_("Ask AI – {service}").format(service=service))

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

	# Conversation options

	def onOptionsAccelerator(self, evt):
		self.openOptions(self._acceleratorFocus.get(evt.GetId()))

	def openOptions(self, focus=None):
		"""Conversation options (modal); nothing changes until OK, then one summary is spoken."""
		conversation = self.conversation
		with ConversationOptionsDialog(self, conversation, focus) as optionsDialog:
			if optionsDialog.ShowModal() != wx.ID_OK:
				return
			summary = optionsDialog.apply()
		self._updateTitle()
		announceWhenFocused(self.questionCtrl if self.FindFocus() is None else self.FindFocus(), summary)

	def onConversations(self, evt):
		from .conversationManager import showConversationManager

		showConversationManager(self)

	def onNewConversation(self, evt):
		if self._dictation is not None:
			self._finishDictation(dictation.REASON_CLOSE)
		self.saveDraft(silent=True)
		if state.current is not None and not state.current.isEmpty:
			state.save(state.current)
		clearScreenContext()
		state.current = state.newConversation()
		self.questionCtrl.Clear()
		self.loadConversation()
		self.updatePendingInput()
		self.questionCtrl.SetFocus()
		# Translators: Announced when a new conversation starts.
		announceWhenFocused(self.questionCtrl, _("New conversation started."))

	# Drafts

	def saveDraft(self, silent=False):
		"""Keep the Question text as the conversation's draft (an empty box clears it)."""
		if not self:
			return
		conversation = self.conversation
		text = self.questionCtrl.GetValue()
		if self._recall.shown is not None and text == self._recall.shown:
			text = self._recall.typed  # A recalled earlier question is not a draft.
		if text.strip() != conversation.draft.strip():
			conversation.draft = text if text.strip() else ""
			conversation.touch()
			state.save(conversation)
		if not silent:
			# Translators: Announced after Ctrl+S saves the Question text as the conversation's draft.
			ui.message(_("Draft saved"))

	# What is attached to the next question

	def _attachedItems(self):
		"""("context", ScreenContext) and ("file", Attachment) in list order."""
		items = []
		if state.pendingContext is not None:
			items.append(("context", state.pendingContext))
		items.extend(("file", item) for item in state.pendingAttachments)
		return items

	def updatePendingInput(self):
		"""Show what is attached; the whole block is hidden (no empty tab stop) when nothing is."""
		if not self:
			return
		context = state.pendingContext
		items = self._attachedItems()
		labels = [contextSummary(item) if kind == "context" else attachmentLabel(item) for kind, item in items]
		selection = self.attachedList.GetSelection()
		self.attachedList.Set(labels)
		if labels:
			self.attachedList.SetSelection(min(max(selection, 0), len(labels) - 1))
		self.screenshotCheck.SetLabel(screenshotLabel(context.windowName if context is not None else ""))
		self.screenshotCheck.SetValue(state.includeScreenshot)
		self.attachedBlock.ShowItems(bool(labels))
		self.screenshotCheck.Show(context is not None)
		self.Layout()
		self.GetSizer().Fit(self)

	def attachContext(self, context):
		"""Main thread: context from "Ask about this" applies to the next question."""
		state.pendingContext = context
		state.includeScreenshot = False
		self.updatePendingInput()
		self.questionCtrl.SetFocus()
		# Translators: Announced after "Ask about this". {summary} is e.g. "Selected text from Word, 1,240 characters".
		announceWhenFocused(self.questionCtrl, _("Attached: {summary}. Type your question.").format(summary=contextSummary(context)))

	def onScreenshotCheck(self, evt):
		state.includeScreenshot = self.screenshotCheck.GetValue()

	def onAttachedListKey(self, evt):
		key = evt.GetKeyCode()
		if key in (wx.WXK_DELETE, wx.WXK_NUMPAD_DELETE):
			self.removeAttached()
			return
		if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
			self.reviewAttached()
			return
		evt.Skip()

	def reviewAttached(self):
		"""Enter on the screen text: show it in a window to read with the arrow keys."""
		index = self.attachedList.GetSelection()
		items = self._attachedItems()
		if 0 <= index < len(items) and items[index][0] == "context":
			context = items[index][1]
			# Translators: Title of the window showing the attached screen text.
			ui.browseableMessage(context.text, _("Attached screen text"))

	def removeAttached(self):
		index = self.attachedList.GetSelection()
		items = self._attachedItems()
		if not (0 <= index < len(items)):
			return
		kind, item = items[index]
		if kind == "context":
			clearScreenContext()
			# Translators: Announced after the attached screen text was removed.
			message = _("Context removed")
		else:
			state.pendingAttachments.remove(index - (1 if state.pendingContext is not None else 0))
			# Translators: Announced after removing an attachment. {name} is the file name.
			message = _("Removed {name}").format(name=item.name)
		self.updatePendingInput()
		target = self.attachedList if self.attachedList.GetCount() else self.questionCtrl
		target.SetFocus()
		announceWhenFocused(target, message)

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
			self.attachedList.SetFocus()
			# Translators: Announced after attaching files. {count} is a number.
			message = ngettext("{count} file attached", "{count} files attached", added).format(count=added)
			announceWhenFocused(self.attachedList, message)

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

	def announceStillAttached(self):
		"""When the dialog opens or comes back to the front: files still waiting for the next question."""
		count = len(state.pendingAttachments)
		if count:
			# Translators: Announced when the dialog opens with files still attached. {count} is a number.
			message = ngettext("Still attached: {count} file", "Still attached: {count} files", count).format(count=count)
			announceWhenFocused(self.questionCtrl, message)

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
			index = 0 if forward else len(matches) - 1
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
		# Translators: Announced for a search match. {number} and {count} are numbers.
		parts.append(_("Match {number} of {count}.").format(number=index + 1, count=len(matches)))
		parts.append(search.lineAt(value, start))
		announceWhenFocused(ctrl, " ".join(parts))

	# Question recall

	def _caretLine(self):
		"""(caret line, number of lines) in the Question box."""
		ctrl = self.questionCtrl
		try:
			result = ctrl.PositionToXY(ctrl.GetInsertionPoint())
			line = result[-1] if isinstance(result, tuple) else 0
			return line, max(1, ctrl.GetNumberOfLines())
		except Exception:
			return 0, 1

	def canRecall(self, older):
		value = self.questionCtrl.GetValue()
		recalled = self._recall.shown is not None and value == self._recall.shown
		line, lines = self._caretLine()
		return history.shouldRecall(older, line, lines, recalled, value)

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
			and self.canRecall(older=key == wx.WXK_UP)
		):
			self.recallQuestion(older=key == wx.WXK_UP)
			return
		if key == wx.WXK_ESCAPE:
			if self._dictation is not None:
				self._finishDictation(dictation.REASON_ESCAPE)
			else:
				# A running question keeps running (Alt+C cancels it); the dialog closes.
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
		announceWhenFocused(control, message)

	# Asking

	def onAskButton(self, evt):
		if state.pending is not None:
			self.cancel(announce=True)
		else:
			if self._dictation is not None:
				self._finishDictation(dictation.REASON_SEND)
			self.ask()

	def _screenshotForSending(self):
		"""(ok, screenshot or None): the screenshot is taken now, only when the box is ticked, from the window
		that was in front when "Ask about this" was used. ok is False when the user chose not to send.
		"""
		context = state.pendingContext
		if context is None or not state.includeScreenshot:
			return True, None
		try:
			from . import png, screenshot

			if not screenshot.windowExists(context.hwnd):
				# Translators: Error when the window for the screenshot was closed. {window} is its title.
				showError(_("{window} is no longer open, so no screenshot can be taken. Untick the screenshot box to send without one.").format(
					window=context.windowName,
				), self)
				return False, None
			captured = screenshot.captureWindow(context.hwnd)
		except Exception:
			log.error("Ask AI: screenshot failed", exc_info=True)
			captured = None
		if captured is None:
			# Translators: Error when no screenshot could be taken.
			showError(_("The screenshot could not be taken. Untick the screenshot box to send without one."), self)
			return False, None
		width, height, pixels = png.downscaleBgra(*captured)
		if png.isNearlyUniform(width, height, pixels):
			# Translators: Yes/No question when the screenshot is a single colour.
			question = _("Screenshot looks blank, possibly because Screen Curtain is on. Send it anyway?")
			# Translators: Title of the blank screenshot question.
			if not askYesNo(question, _("Screenshot"), self):
				return False, None
		return True, (width, height, pixels)

	def ask(self):
		if state.pending is not None:
			# Translators: Reported when the user tries to send while an answer is pending.
			ui.message(_("Still waiting for the answer. Press Alt+C to cancel it."))
			return
		question = self.questionCtrl.GetValue().strip()
		if not question:
			# Translators: Reported when the user sends an empty question.
			ui.message(_("Type or dictate a question first."))
			self.questionCtrl.SetFocus()
			return
		conversation = self.conversation
		refusal = requestRefusal(conversation)
		if refusal:
			showError(refusal, self)
			return
		allowWrites = writeConsent(conversation, self)
		ok, captured = self._screenshotForSending()
		if not ok:
			return
		if state.pendingContext is not None:
			state.pendingContext.screenshot = captured
		longText = len(question) > attachments.LONG_QUESTION_CHARS
		try:
			startRequest(question, conversation, allowWrites=allowWrites)
		except RequestRefused as e:
			showError(e.message, self)
			return
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
		"""Show and speak an answer. doneTitle (the user is in another app) prefixes "Ask AI answer, <title>:"."""
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
		"""Show a failure. quiet (the user is in another app): no dialog; the caller speaks "Failed: ..."."""
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
		folder = existingProjectFolder(conversation) or documentsFolder()
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
			ui.message(_("Still waiting for the answer. Press Alt+C to cancel it first."))
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
		# Screen text and the screenshot choice never wait for a later question; attached files do.
		clearScreenContext()
		if state.pending is not None:
			if getConf()["speakAnswers"]:
				# Translators: Announced when the dialog closes while a question is still running.
				ui.message(_("Still working; the answer will be spoken when ready."))
			else:
				# Translators: Announced when the dialog closes while waiting and speaking is off.
				ui.message(_("Still working; you will hear when the answer is ready."))
		if state.current is not None:
			state.save(state.current)
		self._dictateToken += 1
		self.beepTimer.Stop()
		self.dictationTimer.Stop()
		if AskAIDialog._instance is self:
			AskAIDialog._instance = None
		self.Destroy()


def openAskDialog():
	"""Open the dialog, or bring the existing one to the front. Call on the main thread.
	The first time, ask about the old Ask GPT add-on; whenever it opens or comes back, say which files are
	still attached to the next question.
	"""
	try:
		offerToRemoveOldAddon(gui.mainFrame)
	except Exception:
		log.error("Ask AI: could not check for the old Ask GPT add-on", exc_info=True)
	dialog = AskAIDialog.getInstance()
	gui.mainFrame.prePopup()
	try:
		if dialog is None:
			dialog = AskAIDialog(gui.mainFrame)
			dialog.Show()
		else:
			dialog.Raise()
		dialog.questionCtrl.SetFocus()
		dialog.announceStillAttached()
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
		self._statusTimer = None
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
			_("Show settin&gs for service:"),
			wx.Choice,
			choices=names,
		)
		self.accountCtrl.SetSelection(self._providerIds.index(shown))
		self.accountCtrl.Bind(wx.EVT_CHOICE, self.onAccountChoice)

		# The shown service's controls are grouped, so screen readers say "Claude account and defaults".
		self.accountGroup = wx.StaticBoxSizer(wx.VERTICAL, self, label=self._groupLabel(shown))
		box = self.accountGroup.GetStaticBox()
		group = guiHelper.BoxSizerHelper(box, sizer=self.accountGroup)

		# A single-line read-only TextCtrl is skipped by Tab in wx, so the status is a read-only
		# multi-line box one line high: wx lets Tab reach read-only multi-line boxes.
		self.statusCtrl = group.addLabeledControl(
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
		self.signInButton = signInButtons.addButton(box, label=self._signInLabel(shown))
		self.signInButton.Bind(wx.EVT_BUTTON, self.onSignIn)
		# Translators: Button that checks the sign-in status.
		checkButton = signInButtons.addButton(box, label=_("C&heck status"))
		checkButton.Bind(wx.EVT_BUTTON, lambda evt: self.refreshStatus(announce=True))
		group.addItem(signInButtons)
		accountButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: Button that signs out of the service in its program.
		signOutButton = accountButtons.addButton(box, label=_("Sign ou&t"))
		signOutButton.Bind(wx.EVT_BUTTON, self.onSignOut)
		# Translators: Button that copies the sign-in web address, for when the browser did not open.
		self.copyLinkButton = accountButtons.addButton(box, label=_("Cop&y sign-in link"))
		self.copyLinkButton.Bind(wx.EVT_BUTTON, self.onCopyLink)
		# Translators: Button (Claude only) to type the code the sign-in page shows after using the copied link.
		self.enterCodeButton = accountButtons.addButton(box, label=_("Enter sign-in code..."))
		self.enterCodeButton.Bind(wx.EVT_BUTTON, self.onEnterCode)
		group.addItem(accountButtons)

		# The path label sits on its own line; the field stretches next to Browse.
		# The label is created right before the field, so Windows uses it as the field's name.
		self.pathLabel, self.pathCtrl, browseButton = self._labelledPathRow(
			group,
			self._pathLabel(shown),
			# Translators: Button to choose the program file.
			_("&Browse..."),
			self._values[shown]["path"],
		)
		browseButton.Bind(wx.EVT_BUTTON, self.onBrowse)

		# Editable: the list comes from the service (Codex's own model list, Claude Code's aliases),
		# and any other model name can be typed. The label sits above the box so long names do not widen the panel.
		modelLabel = wx.StaticText(
			box,
			# Translators: Label of the default model field.
			label=_("Default &model (blank uses your plan's default):"),
		)
		self.modelCtrl = wx.ComboBox(
			box,
			choices=[],
			style=wx.CB_DROPDOWN,
			size=(self.scaleSize(PATH_MIN_WIDTH), -1),
		)
		modelBlock = wx.BoxSizer(wx.VERTICAL)
		modelBlock.Add(modelLabel)
		modelBlock.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_VERTICAL)
		modelBlock.Add(self.modelCtrl, flag=wx.EXPAND)
		group.addItem(modelBlock, flag=wx.EXPAND)

		self._effortValues = []
		self.effortCtrl = group.addLabeledControl(
			# Translators: Label of the default reasoning effort choice.
			_("Default &reasoning effort:"),
			wx.Choice,
			choices=[],
		)
		sHelper.addItem(self.accountGroup, flag=wx.EXPAND)

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
		self.announceCheck = sHelper.addItem(
			# Translators: Checkbox: say "Ask AI answer, <conversation>:" or "Failed: ..." when a question
			# finishes while the user works in another application.
			wx.CheckBox(self, label=_("Anno&unce when answers finish while working in other apps")),
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
			# Translators: Checkbox to check GitHub for new Ask AI versions once a day (Tools menu checks now).
			wx.CheckBox(self, label=_("Check for updates automatica&lly")),
		)
		self.autoUpdateCheck.SetValue(conf["autoUpdateCheck"])
		self.removeOldAddonButton = None
		self._oldAddon = findOldAddon()
		if self._oldAddon is not None:
			# Only while Ask GPT 1.x is still installed; no letter is left for it, so it is reached with Tab.
			oldButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
			# Translators: Button in the settings that removes the old Ask GPT add-on.
			self.removeOldAddonButton = oldButtons.addButton(self, label=_("Remove old Ask GPT add-on"))
			self.removeOldAddonButton.Bind(wx.EVT_BUTTON, self.onRemoveOldAddon)
			sHelper.addItem(oldButtons)

		self._loadAccountValues()

	@staticmethod
	def _groupLabel(providerId):
		# Translators: Title of the group of a service's settings. {service} is "ChatGPT" or "Claude".
		return _("{service} account and defaults").format(service=serviceName(providerId))

	@staticmethod
	def _signInLabel(providerId):
		# Translators: Button that starts sign-in in the browser. {service} is "ChatGPT" or "Claude".
		return _("&Sign in to {service}...").format(service=serviceName(providerId))

	@staticmethod
	def _pathLabel(providerId):
		# Translators: Label of the program path field. {program} is "Codex" or "Claude Code".
		return _("{program} e&xecutable path (blank to detect automatically):").format(program=programName(providerId))

	def _labelledPathRow(self, sHelper, label, buttonLabel, value):
		"""Label on its own line, then a stretching text field and a button. Returns (label, field, button).
		The controls belong to the helper's window (the panel, or the account group box).
		"""
		parent = getattr(sHelper, "_parent", None) or self
		labelCtrl = wx.StaticText(parent, label=label)
		field = wx.TextCtrl(parent, size=(self.scaleSize(PATH_MIN_WIDTH), -1))
		field.SetValue(value)
		button = wx.Button(parent, label=buttonLabel)
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
		self.accountGroup.GetStaticBox().SetLabel(self._groupLabel(providerId))
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
		# Arrowing through the services checks only the one the user stays on, 400 ms after the last change,
		# and then says it once: "Claude: Signed in with Claude".
		self._statusGeneration += 1
		self.statusCtrl.SetValue(_("Checking…"))
		if self._statusTimer is not None:
			self._statusTimer.Stop()
		self._statusTimer = wx.CallLater(STATUS_DEBOUNCE_MS, self.refreshStatus, announce=True, withService=True)

	def _updateLoginButtons(self):
		loginRunning = _loginClient is not None and _loginClient.providerId == self._shownProvider
		self.copyLinkButton.Enable(bool(self._loginUrl) and self._loginUrlProvider == self._shownProvider and loginRunning)
		self.enterCodeButton.Enable(loginRunning and providers.get(self._shownProvider).loginAcceptsCode)

	# Status and account

	def _setStatus(self, generation, text, announce, providerId=None):
		if not self or generation != self._statusGeneration:
			return
		self.statusCtrl.SetValue(text)
		initial = self._initialStatusPending
		self._initialStatusPending = False
		if announce and providerId:
			# Translators: Status announced after choosing a service, e.g. "Claude: Signed in with Claude".
			ui.message(_("{service}: {status}").format(service=serviceName(providerId), status=text))
		# Also announce when the user is reading the Status field, so its changes are not missed.
		elif announce or self.FindFocus() == self.statusCtrl:
			ui.message(text)
		elif initial and self.IsShownOnScreen():
			# The first result after the panel opens, while the user is still on it.
			# Translators: Announced when the Ask AI settings open. {status} is e.g. "Signed in with ChatGPT".
			ui.message(_("Ask AI: {status}").format(status=text))

	def refreshStatus(self, announce=False, withService=False):
		self._statusTimer = None
		if not self:
			return
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
			wx.CallAfter(self._setStatus, generation, status, announce, providerId if withService else None)

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
			proc = client.login(
				onUrl=lambda url: wx.CallAfter(self._onLoginUrl, client, url),
				onCodePrompt=lambda: wx.CallAfter(self._updateLoginButtons) if self else None,
			)
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
			client = _loginClient
			wantsCode = (
				providers.get(self._loginUrlProvider).loginAcceptsCode
				and client is not None
				and getattr(client, "loginWantsCode", False)
			)
			if wantsCode:
				# Translators: Announced after copying a sign-in link that ends with a code to type back.
				message = _(
					"Sign-in link copied. Paste it into your browser's address bar. When the page shows a code, "
					"copy it and paste it into the box that opens now."
				)
				# The program asked for a code: open the box for it right away (it stays open until used).
				speakThen(message, lambda: self.onEnterCode(None) if self else None)
				return
			# Translators: Announced after copying the sign-in link.
			ui.message(_("Sign-in link copied. Paste it into your browser's address bar."))
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

	def onRemoveOldAddon(self, evt):
		# Translators: Confirmation before removing the old add-on.
		if not askYesNo(_("Remove the old Ask GPT add-on? Restart NVDA afterwards."), _("Ask AI"), self):
			return
		if self._oldAddon is not None and removeOldAddon(self._oldAddon):
			getConf()["askedToRemoveAskGPT"] = True
			self.removeOldAddonButton.Disable()
			# Translators: Announced after the old add-on was marked for removal.
			ui.message(_("Ask GPT will be removed when NVDA restarts."))

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
