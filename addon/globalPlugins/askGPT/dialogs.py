# -*- coding: UTF-8 -*-
# Ask GPT NVDA add-on: the Ask dialog, the settings panel, the request lifecycle and saved conversations.

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

from . import codexClient, conversations, dictation, exporters, folderSnapshot, modelCatalog
from .codexClient import CodexClient, CodexError
from .textUtils import estimatedSpeechMs, formatTurn, stripMarkdown

addonHandler.initTranslation()

CONFIG_SECTION = "askGPT"
# Set by the global plugin (see codexClient.defaultWorkDir).
workDir = None
# Keep at most this many entries in the Conversation box.
MAX_TRANSCRIPT_ENTRIES = 100
BEEP_INTERVAL_MS = 2000
DICTATION_POLL_MS = 400
# How long to wait for the user to finish signing in in the browser.
LOGIN_TIMEOUT_SECONDS = 180
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
	# Translators: The model choice that lets Codex pick the default model of the user's ChatGPT plan.
	return _("Default (your plan's default)")


class PendingRequest:
	"""A question being answered. Owned by the module, so it survives closing the dialog,
	and tied to the conversation it was asked in.
	"""

	def __init__(self, client, question, conversationId, projectFolder=""):
		self.client = client
		self.question = question
		self.conversationId = conversationId
		self.projectFolder = projectFolder
		self.cancelEvent = threading.Event()


class AppState:
	"""The current conversation, the saved conversations and the running request."""

	def __init__(self):
		self.current = None
		self.pending = None
		self._store = None
		self._storeFolder = None
		self._models = None

	# Conversations

	def conversationsFolder(self):
		configured = codexClient.expandUserPath(getConf()["conversationsDir"])
		return configured or conversations.defaultConversationsDir()

	@property
	def store(self):
		folder = self.conversationsFolder()
		if self._store is None or self._storeFolder != folder:
			self._store = conversations.ConversationStore(
				folder,
				onError=lambda message: log.debugWarning("Ask GPT: %s" % message),
			)
			self._storeFolder = folder
		return self._store

	def newConversation(self):
		conf = getConf()
		return conversations.Conversation(model=conf["model"], reasoningEffort=conf["reasoningEffort"])

	def ensureCurrent(self):
		"""The conversation to show: this session's, else the newest saved one, else a new one."""
		if self.current is None:
			try:
				self.current = self.store.latest()
			except Exception:
				log.error("Ask GPT: could not read saved conversations", exc_info=True)
				self.current = None
			if self.current is None:
				self.current = self.newConversation()
		return self.current

	def save(self, conversation=None):
		"""Save a conversation that has content. Errors are logged and announced, never raised."""
		conversation = conversation or self.current
		if conversation is None or (conversation.isEmpty and not conversation.projectFolder):
			return False
		try:
			self.store.save(conversation)
			return True
		except Exception:
			log.error("Ask GPT: could not save the conversation", exc_info=True)
			# Translators: Reported when a conversation could not be written to disk.
			ui.message(_("Could not save the conversation. Check the conversations folder in the Ask GPT settings."))
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

	@property
	def models(self):
		if self._models is None:
			self.reloadModels()
		return self._models

	def reloadModels(self):
		self._models = modelCatalog.loadModels()
		return self._models


state = AppState()
# The client running `codex login`. Only read and written on the main thread.
_loginClient = None
# The open settings panel, if any, so background results can refresh it.
_activePanel = None
# The running dictation, so shutdown can always restore speech.
_activeDictation = None


def getConf():
	return config.conf[CONFIG_SECTION]


def makeClient(executablePath=None, conversation=None):
	conf = getConf()
	model = conf["model"]
	effort = conf["reasoningEffort"]
	projectFolder = ""
	if conversation is not None:
		model = conversation.model
		effort = modelCatalog.pickEffort(state.models, model, conversation.reasoningEffort or effort)
		projectFolder = conversation.projectFolder
	return CodexClient(
		executablePath=conf["codexPath"] if executablePath is None else executablePath,
		workDir=workDir,
		model=model,
		reasoningEffort=effort,
		timeout=conf["timeout"],
		projectFolder=projectFolder,
		allowProjectWrites=conf["allowProjectWrites"],
	)


def transcriptEntries(conversation):
	"""(text, isAnswer) for the Conversation box: the newest MAX_TRANSCRIPT_ENTRIES messages."""
	entries = []
	for message in conversation.messages[-MAX_TRANSCRIPT_ENTRIES:] if conversation else []:
		role, text = message.get("role"), message.get("text", "")
		if role == conversations.ROLE_USER:
			# Translators: Speaker name for the user's lines in the conversation.
			entries.append((formatTurn(_("You"), text), False))
		elif role == conversations.ROLE_ASSISTANT:
			# Translators: Speaker name for GPT's lines in the conversation.
			entries.append((formatTurn(_("GPT"), stripMarkdown(text) or text), True))
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
		# Translators: Lists files GPT created in the project folder. {files} is a comma-separated list.
		(created, _("Created: {files}")),
		# Translators: Lists files GPT changed in the project folder. {files} is a comma-separated list.
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


def friendlyError(error):
	"""Translated, plain-language message for a CodexError. Never includes technical detail."""
	kind = getattr(error, "kind", codexClient.KIND_OTHER)
	reason = getattr(error, "reason", "")
	if kind == codexClient.KIND_NOT_INSTALLED:
		if reason == codexClient.REASON_CUSTOM_PATH:
			# Translators: Error when the configured Codex path is wrong. {path} is the path.
			return _(
				"The Codex executable path set in the Ask GPT settings does not exist: {path}. "
				"Correct it, or clear it to find Codex automatically."
			).format(path=error.detail)
		if reason == codexClient.REASON_NODE_MISSING:
			# Translators: Error when Node.js, needed by Codex, is missing.
			return _(
				"Node.js was not found. Codex installed with npm needs it: install Node.js from nodejs.org "
				"or with the command winget install OpenJS.NodeJS.LTS, then restart NVDA."
			)
		if reason == codexClient.REASON_INCOMPLETE_INSTALL:
			# Translators: Error when the Codex installation is broken.
			return _(
				'The Codex installation is incomplete. Run "npm install -g @openai/codex" again '
				"in a command prompt, then restart NVDA."
			)
		# Translators: Error when the Codex program cannot be found. Keep the command and address unchanged.
		return _(
			"The OpenAI Codex program was not found. Install Node.js from nodejs.org, then run "
			'"npm install -g @openai/codex" in a command prompt, then restart NVDA.'
		)
	if kind == codexClient.KIND_NOT_LOGGED_IN:
		# Translators: Error when Codex is not signed in.
		return _(
			"Codex is not signed in to your ChatGPT account. "
			"Open NVDA menu, Preferences, Settings, Ask GPT and press Sign in with ChatGPT."
		)
	if kind == codexClient.KIND_USAGE_LIMIT:
		# Translators: Error when the ChatGPT plan's usage limit is reached.
		return _("You have reached the usage limit of your ChatGPT plan. Please try again later.")
	if kind == codexClient.KIND_NETWORK:
		# Translators: Error when ChatGPT cannot be reached.
		return _("Couldn't reach ChatGPT. Check your internet connection and try again.")
	if kind == codexClient.KIND_TIMEOUT:
		# Translators: Error when no answer came in time.
		return _("No answer arrived in time. Try again, or increase the timeout in the Ask GPT settings.")
	if kind == codexClient.KIND_CANCELLED:
		# Translators: Reported when the user cancels a question.
		return _("Request cancelled.")
	if kind == codexClient.KIND_CONFIG:
		if reason == codexClient.REASON_PROJECT_MISSING:
			# Translators: Error when the project folder was moved or deleted. {path} is the folder.
			return _("The project folder no longer exists: {path}. Choose another folder or clear it.").format(
				path=error.detail,
			)
		# Translators: Error about an invalid setting. {message} explains it.
		return _("A setting is not valid: {message}").format(message=error.message)
	if reason == codexClient.REASON_EMPTY_ANSWER:
		# Translators: Error when GPT returned nothing.
		return _("GPT returned an empty answer. Please try asking again.")
	# For other failures the client's message is already plain language without technical detail.
	return getattr(error, "message", "") or _("Something went wrong while asking GPT.")


def transcriptErrorText(error):
	"""Friendly message, then the technical detail after a label, for the Conversation box only."""
	text = friendlyError(error)
	detail = getattr(error, "detail", "")
	if detail and getattr(error, "reason", "") not in (codexClient.REASON_CUSTOM_PATH, codexClient.REASON_PROJECT_MISSING):
		# Translators: Introduces technical error details in the conversation.
		text += " " + _("Technical details: {detail}").format(detail=detail)
	return text


def showError(message, parent=None):
	# Translators: Title of error dialogs.
	title = _("Ask GPT error")
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

		speech.speak([message, CallbackCommand(lambda: wx.CallAfter(callback), name="askGPT.afterSpeech")])
		try:
			import braille

			braille.handler.message(message)
		except Exception:
			pass
	except Exception:
		log.debugWarning("Ask GPT: speech callback unavailable", exc_info=True)
		ui.message(message)
	wx.CallLater(estimatedSpeechMs(message), callback)


def openSettings():
	gui.mainFrame.popupSettingsDialog(NVDASettingsDialog, AskGPTSettingsPanel)


def documentsFolder():
	"""Documents\\Ask GPT, using the real (possibly redirected) Documents folder."""
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
		log.error("Ask GPT: saving failed", exc_info=True)
		# Translators: Error when a file could not be saved. {error} is the reason.
		showError(_("The file could not be saved: {error}").format(error=e), parent)
		return None
	# Translators: Announced after saving a file. {name} is the file name, {folder} the folder.
	ui.message(_("Saved {name} in {folder}").format(name=os.path.basename(path), folder=folder))
	return path


# Request lifecycle (main thread unless noted)


def startRequest(question, conversation):
	conf = getConf()
	history = conversation.history if conf["keepContext"] else []
	prompt = codexClient.buildPrompt(
		question,
		history,
		conf["instructions"],
		projectFolder=bool(conversation.projectFolder),
	)
	conversation.addMessage(conversations.ROLE_USER, question)
	pending = PendingRequest(makeClient(conversation=conversation), question, conversation.id, conversation.projectFolder)
	state.pending = pending
	threading.Thread(target=_requestWorker, args=(pending, prompt), name="askGPT.ask", daemon=True).start()
	return pending


def _requestWorker(pending, prompt):
	"""Background thread. Snapshots the project folder around the request to find new files."""
	before = folderSnapshot.snapshot(pending.projectFolder) if pending.projectFolder else None
	try:
		answer = pending.client.ask(prompt, pending.cancelEvent)
	except CodexError as e:
		if e.kind != codexClient.KIND_CANCELLED:
			log.debugWarning("Ask GPT: %s: %s | %s" % (e.kind, e.message, e.detail))
		wx.CallAfter(_onRequestDone, pending, None, e, (), ())
		return
	except Exception as e:
		log.error("Ask GPT: unexpected error", exc_info=True)
		error = CodexError(codexClient.KIND_OTHER, "Something went wrong while asking GPT.", str(e))
		wx.CallAfter(_onRequestDone, pending, None, error, (), ())
		return
	created, changed = (), ()
	if before is not None:
		try:
			created, changed = folderSnapshot.compare(before, folderSnapshot.snapshot(pending.projectFolder))
		except Exception:
			log.debugWarning("Ask GPT: could not compare the project folder", exc_info=True)
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


def _onRequestDone(pending, answer, error, created, changed):
	if pending is not state.pending:
		return  # Cancelled.
	state.pending = None
	conversation = state.conversationById(pending.conversationId)
	if conversation is None:
		log.debugWarning("Ask GPT: the conversation of a finished request was deleted")
		return
	isCurrent = conversation is state.current
	dialog = AskGPTDialog.getInstance()
	if error is None:
		conversation.addMessage(conversations.ROLE_ASSISTANT, answer)
		changes = fileChangesText(created, changed)
		if changes:
			conversation.addMessage(conversations.ROLE_NOTE, changes)
		state.save(conversation)
		spoken = stripMarkdown(answer) or answer
		if dialog is not None and isCurrent:
			dialog.onAnswer(spoken, changes)
		elif getConf()["speakAnswers"]:
			ui.message(spoken)
			if changes:
				ui.message(changes)
		else:
			# Translators: Announced when an answer arrives after the dialog was closed and speaking is off.
			ui.message(_("GPT's answer is ready. Press NVDA+Alt+Shift+G to hear it."))
		return
	conversation.addMessage(conversations.ROLE_ERROR, transcriptErrorText(error))
	state.save(conversation)
	if dialog is not None and isCurrent:
		dialog.onFailure(pending.question, error)
	else:
		ui.message(friendlyError(error))


def switchConversation(conversation):
	"""Make conversation current (main thread). The running request keeps its own conversation."""
	if state.current is not None and state.current is not conversation:
		state.save(state.current)
	state.current = conversation
	dialog = AskGPTDialog.getInstance()
	if dialog is not None:
		dialog.loadConversation()


def cancelLogin():
	"""Stop a running `codex login` synchronously. Main thread."""
	global _loginClient
	client, _loginClient = _loginClient, None
	if client is not None:
		client.cancelLogin()


def _finishLogin(client, timedOut):
	"""Main thread, after `codex login` exited or timed out. Compare-and-clear avoids races with a
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
			loggedIn, text = client.loginStatus()
		except Exception:
			log.debugWarning("Ask GPT: status check after sign-in failed", exc_info=True)
			loggedIn, text = False, ""
		wx.CallAfter(_announceLoginResult, loggedIn, text)

	threading.Thread(target=check, name="askGPT.loginCheck", daemon=True).start()


def _announceLoginResult(loggedIn, text):
	if loggedIn and codexClient.isApiKeyLogin(text):
		# Translators: Announced when Codex is signed in with an API key instead of ChatGPT.
		ui.message(_("Codex is signed in with an API key, not with ChatGPT."))
	elif loggedIn:
		# Translators: Announced when sign-in succeeded.
		ui.message(_("Signed in with ChatGPT."))
	else:
		# Translators: Announced when sign-in did not complete.
		ui.message(_("Sign-in was not completed. Press Sign in to try again."))
	_refreshPanel()


def _refreshPanel():
	panel = _activePanel
	if panel is not None and panel:
		panel.refreshStatus()


def restoreDictation():
	"""Give NVDA its speech back if a dictation is running. Safe to call any time."""
	global _activeDictation
	session, _activeDictation = _activeDictation, None
	if session is not None:
		try:
			session.finish(dictation.REASON_CLOSE)
		except Exception:
			log.error("Ask GPT: could not restore the speech mode", exc_info=True)


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


class AskGPTDialog(DpiScalingHelperMixinWithoutInit, wx.Dialog):
	"""Non-modal dialog for asking questions. Only one instance exists at a time.
	Mnemonics: Q A/C D V O S M E J F X L R N (Cancel shares the Ask button).
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
		# Translators: Title of the Ask GPT dialog.
		super().__init__(parent, title=_("Ask GPT"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		AskGPTDialog._instance = self
		self._busy = False
		self._dictateToken = 0
		self._dictation = None
		self._lastAnswerPos = None
		self._shownEntries = []
		self._modelSlugs = []
		self._effortValues = []
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
		self.saveButton = answerButtons.addButton(self, label=_("&Save answer as..."))
		self.saveButton.Bind(wx.EVT_BUTTON, self.onSaveAnswer)
		sHelper.addItem(answerButtons)

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
		# Translators: Button to choose the project folder where GPT may read and save files.
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
		self.loadConversation()
		if state.pending is not None:
			self._setBusy(True)  # A question asked before the dialog was closed is still running.
		self.questionCtrl.SetFocus()

	# Conversation

	@property
	def conversation(self):
		return state.ensureCurrent()

	def loadConversation(self):
		"""Show the current conversation: title, Conversation box, model, effort and project folder."""
		conversation = self.conversation
		self._updateTitle()
		self._shownEntries = []
		self._refreshConversationBox(rebuild=True)
		if self._lastAnswerPos is not None:
			# Opening or switching lands on the beginning of the newest answer.
			self.conversationCtrl.SetInsertionPoint(self._lastAnswerPos)
			self.conversationCtrl.ShowPosition(self._lastAnswerPos)
		self._fillModelChoices()
		self._updateProject()
		return conversation

	def _updateTitle(self):
		title = self.conversation.title
		# Translators: Title of the Ask GPT dialog with the conversation title, e.g. "Ask GPT - Trip to Paris".
		self.SetTitle(_("Ask GPT - {title}").format(title=title) if title else _("Ask GPT"))

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

	def _fillModelChoices(self):
		conversation = self.conversation
		conf = getConf()
		models = modelCatalog.modelChoices(state.models, extraSlugs=[conversation.model, conf["model"]])
		self._modelSlugs = [""] + [model.slug for model in models]
		self.modelChoice.Set([defaultModelLabel()] + [model.displayName for model in models])
		self.modelChoice.SetSelection(self._modelSlugs.index(conversation.model) if conversation.model in self._modelSlugs else 0)
		self._fillEffortChoices()

	def _fillEffortChoices(self):
		conversation = self.conversation
		efforts = modelCatalog.effortsFor(state.models, conversation.model)
		wanted = conversation.reasoningEffort or getConf()["reasoningEffort"]
		effort = modelCatalog.pickEffort(state.models, conversation.model, wanted)
		self._effortValues = list(efforts)
		self.effortChoice.Set([effortLabel(value) for value in efforts])
		self.effortChoice.SetSelection(self._effortValues.index(effort) if effort in self._effortValues else 0)

	def _modelName(self, slug):
		model = modelCatalog.findModel(state.models, slug) if slug else None
		if model is not None:
			return model.displayName
		return slug or defaultModelLabel()

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
			state.models,
			slug,
			conversation.reasoningEffort or getConf()["reasoningEffort"],
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
			log.error("Ask GPT: could not open the project folder", exc_info=True)
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

	# Keyboard

	def onCharHook(self, evt):
		key = evt.GetKeyCode()
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
					# Speech comes back first, so "Asking GPT" is heard.
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
		startRequest(question, self.conversation)
		self._updateTitle()
		self._refreshConversationBox()
		self.questionCtrl.Clear()
		self._setBusy(True)
		# Translators: Announced while waiting for an answer.
		ui.message(_("Asking GPT…"))

	def onAnswer(self, spoken, changes):
		self._setBusy(False)
		self._refreshConversationBox()
		if getConf()["speakAnswers"]:
			ui.message(spoken)
		else:
			# Translators: Announced when an answer arrives and automatic speaking is off.
			ui.message(_("Answer received. It is in the conversation box."))
		if changes:
			ui.message(changes)

	def onFailure(self, question, error):
		self._setBusy(False)
		self._refreshConversationBox()
		if not self.questionCtrl.GetValue().strip():
			self.questionCtrl.SetValue(question)
			self.questionCtrl.SetInsertionPointEnd()
		if error.kind == codexClient.KIND_NOT_LOGGED_IN:
			# Translators: Yes/No question after a sign-in error.
			offer = _("Codex is not signed in to ChatGPT; open the Ask GPT settings to sign in?")
		elif error.kind == codexClient.KIND_NOT_INSTALLED:
			# Translators: Yes/No question after Codex could not be started.
			offer = _("The Codex program could not be started; open the Ask GPT settings?")
		else:
			showError(friendlyError(error), self)
			return
		if askYesNo(offer, _("Ask GPT"), self):
			openSettings()

	def cancel(self, announce=False):
		pending = cancelRequest()
		if pending is None:
			return
		self._setBusy(False)
		self._refreshConversationBox()
		if not self.questionCtrl.GetValue().strip():
			self.questionCtrl.SetValue(pending.question)
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
			log.error("Ask GPT: could not switch speech off for dictation", exc_info=True)
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
			log.error("Ask GPT: dictation check failed", exc_info=True)
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
			log.error("Ask GPT: could not restore the speech mode", exc_info=True)
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
			log.error("Ask GPT: could not send Windows+H", exc_info=True)
			# Translators: Reported when voice typing could not be started.
			ui.message(_("Could not start Windows voice typing. Press Windows+H yourself."))
			return False

	# Closing

	def onClose(self, evt):
		self._finishDictation(dictation.REASON_CLOSE)
		if state.pending is not None:
			if getConf()["speakAnswers"]:
				# Translators: Announced when the dialog is closed while waiting for an answer.
				ui.message(_("GPT will speak the answer when it's ready."))
			else:
				# Translators: Announced when the dialog is closed while waiting and speaking is off.
				ui.message(_("GPT is still working. You will hear when the answer is ready."))
		if state.current is not None:
			state.save(state.current)
		self._dictateToken += 1
		self.beepTimer.Stop()
		self.dictationTimer.Stop()
		if AskGPTDialog._instance is self:
			AskGPTDialog._instance = None
		self.Destroy()


def openAskDialog():
	"""Open the dialog, or bring the existing one to the front. Call on the main thread."""
	dialog = AskGPTDialog.getInstance()
	gui.mainFrame.prePopup()
	try:
		if dialog is None:
			dialog = AskGPTDialog(gui.mainFrame)
			dialog.Show()
		else:
			dialog.Raise()
		dialog.questionCtrl.SetFocus()
	finally:
		gui.mainFrame.postPopup()


def closeAskDialog():
	dialog = AskGPTDialog.getInstance()
	if dialog is not None:
		dialog.Close()


def openConversationManager():
	"""Open the conversation manager from anywhere (Tools menu or gesture). Main thread."""
	from .conversationManager import showConversationManager

	dialog = AskGPTDialog.getInstance()
	if dialog is not None:
		dialog.Raise()
		showConversationManager(dialog)
		return
	gui.mainFrame.prePopup()
	try:
		showConversationManager(gui.mainFrame)
	finally:
		gui.mainFrame.postPopup()


class AskGPTSettingsPanel(SettingsPanel):
	# Mnemonics used here: S, H, U, Y, X, B, M, R, I, P, K, W, T, J, F, E, D.
	# The NVDA Settings dialog itself owns O (OK), C (Cancel, Categories) and A (Apply).

	# Translators: Title of the Ask GPT category in NVDA settings.
	title = _("Ask GPT")
	# Translators: Description of the Ask GPT settings, read when the category opens.
	panelDescription = _(
		"Requires the free OpenAI Codex CLI: install Node.js, then run "
		'"npm install -g @openai/codex" in a command prompt '
		"(or download codex from github.com/openai/codex/releases). "
		"Uses your ChatGPT Plus, Pro or Team plan; no API key needed."
	)

	def makeSettings(self, settingsSizer):
		global _activePanel
		_activePanel = self
		self.Bind(wx.EVT_WINDOW_DESTROY, self._onDestroy)
		conf = getConf()
		self._statusGeneration = 0
		self._loginUrl = None
		self._initialStatusPending = True
		self._models = state.reloadModels()
		sHelper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		description = sHelper.addItem(wx.StaticText(self, label=self.panelDescription))
		description.Wrap(self.scaleSize(PANEL_CONTENT_WIDTH))

		# A single-line read-only TextCtrl is skipped by Tab in wx, so the status is a read-only
		# multi-line box one line high: wx lets Tab reach read-only multi-line boxes.
		# It is the first control that takes focus in the panel.
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

		# Two rows of two buttons, so the row fits NVDA's settings area.
		signInButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: Button that starts ChatGPT sign-in in the browser.
		signInButton = signInButtons.addButton(self, label=_("&Sign in with ChatGPT..."))
		signInButton.Bind(wx.EVT_BUTTON, self.onSignIn)
		# Translators: Button that checks the sign-in status.
		checkButton = signInButtons.addButton(self, label=_("C&heck status"))
		checkButton.Bind(wx.EVT_BUTTON, lambda evt: self.refreshStatus(announce=True))
		sHelper.addItem(signInButtons)
		accountButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: Button that signs out of ChatGPT in Codex.
		signOutButton = accountButtons.addButton(self, label=_("Sign o&ut"))
		signOutButton.Bind(wx.EVT_BUTTON, self.onSignOut)
		# Translators: Button that copies the sign-in web address, for when the browser did not open.
		self.copyLinkButton = accountButtons.addButton(self, label=_("Cop&y sign-in link"))
		self.copyLinkButton.Bind(wx.EVT_BUTTON, self.onCopyLink)
		self.copyLinkButton.Disable()
		sHelper.addItem(accountButtons)

		# The path label sits on its own line; the field stretches next to Browse.
		# The label is created right before the field, so Windows uses it as the field's name.
		self.pathCtrl, browseButton = self._labelledPathRow(
			sHelper,
			# Translators: Label of the Codex program path field.
			_("Codex e&xecutable path (leave blank to detect automatically):"),
			# Translators: Button to choose the Codex program file.
			_("&Browse..."),
			conf["codexPath"],
		)
		browseButton.Bind(wx.EVT_BUTTON, self.onBrowse)

		# Editable: the list comes from Codex's own model list, and any other model name can be typed.
		# The label sits above the box so long model names do not widen the panel.
		self._modelLabels = [""] + [modelCatalog.comboLabel(model) for model in self._models]
		modelLabel = wx.StaticText(
			self,
			# Translators: Label of the default model field.
			label=_("Default &model (blank uses your plan's default):"),
		)
		self.modelCtrl = wx.ComboBox(
			self,
			choices=self._modelLabels,
			style=wx.CB_DROPDOWN,
			size=(self.scaleSize(PATH_MIN_WIDTH), -1),
		)
		modelBlock = wx.BoxSizer(wx.VERTICAL)
		modelBlock.Add(modelLabel)
		modelBlock.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_VERTICAL)
		modelBlock.Add(self.modelCtrl, flag=wx.EXPAND)
		sHelper.addItem(modelBlock, flag=wx.EXPAND)
		configuredModel = modelCatalog.findModel(self._models, conf["model"])
		self.modelCtrl.SetValue(modelCatalog.comboLabel(configuredModel) if configuredModel else conf["model"])

		self._effortValues = list(codexClient.REASONING_EFFORTS)
		for model in self._models:
			for effort in model.efforts:
				if effort not in self._effortValues:
					self._effortValues.append(effort)
		effort = conf["reasoningEffort"]
		if effort and effort not in self._effortValues:
			self._effortValues.append(effort)
		self.effortCtrl = sHelper.addLabeledControl(
			# Translators: Label of the default reasoning effort choice.
			_("Default &reasoning effort:"),
			wx.Choice,
			choices=[effortLabel(value) for value in self._effortValues],
		)
		self.effortCtrl.SetSelection(self._effortValues.index(effort) if effort in self._effortValues else 0)

		# Label above the box, which then uses the full panel width without widening the panel.
		instructionsLabel = wx.StaticText(
			self,
			# Translators: Label of the instructions (system prompt) field.
			label=_("&Instructions for GPT:"),
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
			# Translators: Checkbox that lets GPT create and edit files in a conversation's project folder.
			wx.CheckBox(self, label=_("Allow GPT to create and edit files in the pro&ject folder")),
		)
		self.projectWritesCheck.SetValue(conf["allowProjectWrites"])

		self.timeoutCtrl = sHelper.addLabeledControl(
			# Translators: Label of the timeout spin control.
			_("Answer &timeout in seconds:"),
			nvdaControls.SelectOnFocusSpinCtrl,
			min=codexClient.MIN_TIMEOUT,
			max=codexClient.MAX_TIMEOUT,
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

		self.conversationsDirCtrl, chooseFolderButton = self._labelledPathRow(
			sHelper,
			# Translators: Label of the folder where conversations are saved. Blank means the default folder.
			_("Conversations &folder (blank for the default):"),
			# Translators: Button to choose the conversations folder.
			_("Choos&e folder..."),
			conf["conversationsDir"],
		)
		chooseFolderButton.Bind(wx.EVT_BUTTON, self.onChooseConversationsDir)

	def _labelledPathRow(self, sHelper, label, buttonLabel, value):
		"""Label on its own line, then a stretching text field and a button."""
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
		return field, button

	def _onDestroy(self, evt):
		global _activePanel
		if evt.GetEventObject() is self and _activePanel is self:
			_activePanel = None
		evt.Skip()

	def onPanelActivated(self):
		super().onPanelActivated()
		self.refreshStatus(announce=False)

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
			# Translators: Announced when the Ask GPT settings open. {status} is e.g. "Signed in with ChatGPT".
			ui.message(_("Ask GPT: {status}").format(status=text))

	def refreshStatus(self, announce=False):
		self._statusGeneration += 1
		generation = self._statusGeneration
		self.statusCtrl.SetValue(_("Checking…"))
		client = makeClient(executablePath=self.pathCtrl.GetValue())

		def worker():
			try:
				loggedIn, text = client.loginStatus()
			except CodexError as e:
				log.debugWarning("Ask GPT: status check: %s: %s | %s" % (e.kind, e.message, e.detail))
				status = shortStatusForError(e)
			except Exception:
				log.error("Ask GPT: status check failed", exc_info=True)
				# Translators: Status when the check failed.
				status = _("Could not check the status")
			else:
				if loggedIn and codexClient.isApiKeyLogin(text):
					# Translators: Status when Codex uses an API key instead of a ChatGPT account.
					status = _("Signed in with an API key, which is billed per use")
				elif loggedIn:
					# Translators: Status when signed in.
					status = _("Signed in with ChatGPT")
				else:
					# Translators: Status when not signed in.
					status = _("Not signed in")
			wx.CallAfter(self._setStatus, generation, status, announce)

		threading.Thread(target=worker, name="askGPT.status", daemon=True).start()

	def onSignIn(self, evt):
		global _loginClient
		# Kill any running login before starting a new one.
		cancelLogin()
		client = makeClient(executablePath=self.pathCtrl.GetValue())
		try:
			client.locate()
		except CodexError as e:
			showError(friendlyError(e), self)
			self.refreshStatus()
			return
		self._loginUrl = None
		self.copyLinkButton.Disable()
		self._statusGeneration += 1
		# Translators: Status while the user signs in in the browser.
		self.statusCtrl.SetValue(_("Waiting for you to sign in in your browser"))
		# Translators: Announced before the browser opens for sign-in.
		message = _(
			"Your browser will open. Sign in with your ChatGPT account, then come back to NVDA. "
			"NVDA will tell you when you are signed in."
		)
		_loginClient = client
		# Start the browser only after the message, so the focus change does not cut it off.
		speakThen(message, lambda: self._startLogin(client))

	def _startLogin(self, client):
		if _loginClient is not client:
			return  # Cancelled, superseded or shut down while the message was spoken.
		try:
			proc = client.login(onUrl=lambda url: wx.CallAfter(self._onLoginUrl, client, url))
		except CodexError as e:
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

		threading.Thread(target=waiter, name="askGPT.login", daemon=True).start()

	def _onLoginUrl(self, client, url):
		if not self or _loginClient is not client:
			return
		self._loginUrl = url
		self.copyLinkButton.Enable()

	def onCopyLink(self, evt):
		if not self._loginUrl:
			return
		if api.copyToClip(self._loginUrl):
			# Translators: Announced after copying the sign-in link.
			ui.message(_("Sign-in link copied. Paste it into your browser's address bar."))
		else:
			ui.message(_("Could not copy to the clipboard."))

	def onSignOut(self, evt):
		# Translators: Confirmation before signing out.
		if not askYesNo(_("Sign out of ChatGPT in Codex?"), _("Ask GPT"), self):
			return
		cancelLogin()
		self._statusGeneration += 1
		generation = self._statusGeneration
		client = makeClient(executablePath=self.pathCtrl.GetValue())

		def worker():
			try:
				ok, text = client.logout()
				# Translators: Status after signing out.
				status = _("Signed out") if ok else (text.splitlines()[0] if text else _("Not signed in"))
			except CodexError as e:
				log.debugWarning("Ask GPT: sign out: %s: %s | %s" % (e.kind, e.message, e.detail))
				status = shortStatusForError(e)
			except Exception:
				log.error("Ask GPT: sign out failed", exc_info=True)
				# Translators: Status when signing out failed.
				status = _("Could not sign out")
			wx.CallAfter(self._setStatus, generation, status, True)

		threading.Thread(target=worker, name="askGPT.logout", daemon=True).start()

	def onBrowse(self, evt):
		currentPath = codexClient.expandUserPath(self.pathCtrl.GetValue())
		with wx.FileDialog(
			self,
			# Translators: Title of the file chooser for the Codex program.
			_("Select the Codex program"),
			defaultDir=os.path.dirname(currentPath),
			# Translators: File type filter in the file chooser.
			wildcard=_("Programs (*.exe;*.cmd)|*.exe;*.cmd|All files (*.*)|*.*"),
			style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
		) as fileDialog:
			if fileDialog.ShowModal() == wx.ID_OK:
				self.pathCtrl.SetValue(fileDialog.GetPath())
				self.refreshStatus(announce=True)

	def onChooseConversationsDir(self, evt):
		current = codexClient.expandUserPath(self.conversationsDirCtrl.GetValue()) or state.conversationsFolder()
		with wx.DirDialog(
			self,
			# Translators: Title of the folder chooser for saved conversations.
			_("Choose where Ask GPT saves conversations"),
			defaultPath=current if os.path.isdir(current) else "",
			style=wx.DD_DEFAULT_STYLE,
		) as folderDialog:
			if folderDialog.ShowModal() == wx.ID_OK:
				self.conversationsDirCtrl.SetValue(folderDialog.GetPath())

	# Saving

	def _modelSlug(self):
		return modelCatalog.slugFromComboText(self.modelCtrl.GetValue(), self._models)

	def isValid(self):
		rawPath = self.pathCtrl.GetValue().strip()
		# Only check the path when it was changed, so an unplugged drive does not block saving other settings.
		if rawPath and rawPath != getConf()["codexPath"]:
			path = codexClient.expandUserPath(rawPath)
			if not os.path.isfile(path):
				# Translators: Error when the Codex path does not exist.
				showError(_("The Codex executable path does not exist: {path}").format(path=path), self)
				self.pathCtrl.SetFocus()
				return False
		if not codexClient.isValidModel(self._modelSlug()):
			showError(
				# Translators: Error when the model name has invalid characters.
				_("The model name may only contain letters, digits, dots, dashes, colons and slashes."),
				self,
			)
			self.modelCtrl.SetFocus()
			return False
		rawFolder = self.conversationsDirCtrl.GetValue().strip()
		if rawFolder and rawFolder != getConf()["conversationsDir"]:
			folder = codexClient.expandUserPath(rawFolder)
			try:
				os.makedirs(folder, exist_ok=True)
			except OSError as e:
				# Translators: Error when the conversations folder cannot be used. {error} is the reason.
				showError(_("The conversations folder cannot be used: {error}").format(error=e), self)
				self.conversationsDirCtrl.SetFocus()
				return False
		return super().isValid()

	def onSave(self):
		conf = getConf()
		conf["codexPath"] = self.pathCtrl.GetValue().strip()
		conf["model"] = self._modelSlug()
		selection = self.effortCtrl.GetSelection()
		conf["reasoningEffort"] = self._effortValues[selection] if selection >= 0 else "low"
		instructions = self.instructionsCtrl.GetValue().strip()
		conf["instructions"] = instructions or codexClient.DEFAULT_INSTRUCTIONS
		conf["speakAnswers"] = self.speakCheck.GetValue()
		conf["keepContext"] = self.contextCheck.GetValue()
		conf["beepWhileWaiting"] = self.beepCheck.GetValue()
		conf["allowProjectWrites"] = self.projectWritesCheck.GetValue()
		conf["timeout"] = codexClient.clampTimeout(self.timeoutCtrl.GetValue())
		conf["dictationSilenceTimeout"] = int(dictation.clampSilenceTimeout(self.dictationTimeoutCtrl.GetValue()))
		conf["conversationsDir"] = self.conversationsDirCtrl.GetValue().strip()


def shortStatusForError(error):
	"""One short sentence for the Status field."""
	if error.kind == codexClient.KIND_NOT_INSTALLED:
		if error.reason == codexClient.REASON_NODE_MISSING:
			# Translators: Status when Node.js is missing.
			return _("Node.js not found")
		if error.reason == codexClient.REASON_CUSTOM_PATH:
			# Translators: Status when the configured path is wrong.
			return _("Codex executable path not found")
		# Translators: Status when the Codex program cannot be found.
		return _("Codex CLI not found")
	if error.kind == codexClient.KIND_NETWORK:
		# Translators: Status when ChatGPT cannot be reached.
		return _("Could not reach ChatGPT")
	if error.kind == codexClient.KIND_TIMEOUT:
		# Translators: Status when the check took too long.
		return _("The status check timed out")
	# Translators: Status when the check failed.
	return _("Could not check the status")
