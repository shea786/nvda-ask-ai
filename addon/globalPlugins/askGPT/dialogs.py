# -*- coding: UTF-8 -*-
# Ask GPT NVDA add-on: the Ask dialog and the settings panel.

import os
import threading

import wx

import addonHandler
import api
import config
import gui
import tones
import ui
from gui import guiHelper, nvdaControls
from gui.settingsDialogs import NVDASettingsDialog, SettingsPanel
from logHandler import log

from . import codexClient
from .codexClient import CodexClient, CodexError
from .textUtils import formatTurn, stripMarkdown

addonHandler.initTranslation()

CONFIG_SECTION = "askGPT"
# Set by the global plugin to a folder inside NVDA's configuration directory.
workDir = None
# Keep at most this many messages (questions plus answers) in memory.
MAX_STORED_MESSAGES = 100
BEEP_INTERVAL_MS = 2000


class ConversationState:
	"""Conversation shared between dialog instances, so closing the dialog keeps the context."""

	def __init__(self):
		self.history = []  # (role, text), role is "user" or "assistant".
		self.transcript = []  # Lines shown in the conversation view.
		self.lastAnswer = ""

	def clear(self):
		self.history = []
		self.transcript = []
		self.lastAnswer = ""

	def addExchange(self, question, answer):
		self.history.append(("user", question))
		self.history.append(("assistant", answer))
		del self.history[:-MAX_STORED_MESSAGES]


state = ConversationState()
_loginClient = None


def getConf():
	return config.conf[CONFIG_SECTION]


def makeClient(executablePath=None):
	conf = getConf()
	return CodexClient(
		executablePath=conf["codexPath"] if executablePath is None else executablePath,
		workDir=workDir,
		model=conf["model"],
		reasoningEffort=conf["reasoningEffort"],
		timeout=conf["timeout"],
	)


def friendlyError(error):
	"""Translated, human readable message for a CodexError."""
	kind = getattr(error, "kind", codexClient.KIND_OTHER)
	installHelp = _(
		# Translators: How to install the Codex program. Keep the command and web address unchanged.
		'Install Node.js from nodejs.org, then run "npm install -g @openai/codex" in a command prompt, '
		"or download codex from github.com/openai/codex/releases. Then restart NVDA, "
		"or set the Codex executable path in NVDA menu, Preferences, Settings, Ask GPT."
	)
	if kind == codexClient.KIND_NOT_INSTALLED:
		# Translators: Error when the Codex program cannot be found. {help} explains how to install it.
		return _("The OpenAI Codex program was not found or could not start. {help}").format(help=installHelp)
	if kind == codexClient.KIND_NOT_LOGGED_IN:
		# Translators: Error when Codex is not signed in.
		return _(
			"Codex is not signed in to your ChatGPT account. "
			"Open NVDA menu, Preferences, Settings, Ask GPT and press Sign in with ChatGPT."
		)
	if kind == codexClient.KIND_USAGE_LIMIT:
		# Translators: Error when the ChatGPT plan's usage limit is reached.
		return _("You have reached the usage limit of your ChatGPT plan. Please try again later.")
	if kind == codexClient.KIND_TIMEOUT:
		# Translators: Error when no answer came in time.
		return _(
			"No answer arrived in time. Try again, or increase the timeout in the Ask GPT settings."
		)
	if kind == codexClient.KIND_CANCELLED:
		# Translators: Reported when the user cancels a question.
		return _("Request cancelled.")
	if kind == codexClient.KIND_CONFIG:
		# Translators: Error about an invalid setting. {detail} is the technical explanation.
		return _("A setting is not valid: {detail}").format(detail=error)
	detail = getattr(error, "detail", "") or str(error)
	# Translators: Generic error. {detail} is the technical explanation.
	return _("Something went wrong while asking GPT. {detail}").format(detail=detail).strip()


def showError(message, parent=None):
	# Translators: Title of error dialogs.
	title = _("Ask GPT error")
	try:
		from gui.message import MessageDialog  # NVDA 2025.1 and later.

		MessageDialog.alert(message, title, parent)
	except ImportError:
		gui.messageBox(message, title, wx.OK | wx.ICON_ERROR, parent)


def askYesNo(message, title, parent=None):
	try:
		from gui.message import MessageDialog, ReturnCode  # NVDA 2025.1 and later.
	except ImportError:
		return gui.messageBox(message, title, wx.YES_NO | wx.ICON_QUESTION, parent) == wx.YES
	return MessageDialog.ask(message, title, parent) == ReturnCode.YES


def openSettings():
	gui.mainFrame.popupSettingsDialog(NVDASettingsDialog, AskGPTSettingsPanel)


def cancelLogin():
	global _loginClient
	if _loginClient is not None:
		_loginClient.cancelLogin()
		_loginClient = None


def _announceLoginResult(loggedIn, text):
	if loggedIn and codexClient.isApiKeyLogin(text):
		# Translators: Announced when Codex is signed in with an API key instead of ChatGPT.
		ui.message(_("Codex is signed in with an API key, not with ChatGPT."))
	elif loggedIn:
		# Translators: Announced when sign-in succeeded.
		ui.message(_("Signed in with ChatGPT."))
	else:
		# Translators: Announced when sign-in did not complete.
		ui.message(_("Not signed in. You can press Sign in with ChatGPT to try again."))


class AskGPTDialog(wx.Dialog):
	"""Non-modal dialog for asking questions. Only one instance exists at a time."""

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
		self._requestId = 0
		self._cancelEvent = None
		self._busy = False
		self._pendingQuestion = ""
		self._dictateToken = 0
		self.beepTimer = wx.Timer(self)

		mainSizer = wx.BoxSizer(wx.VERTICAL)
		sHelper = guiHelper.BoxSizerHelper(self, orientation=wx.VERTICAL)

		self.questionCtrl = sHelper.addLabeledControl(
			# Translators: Label of the box where the question is typed.
			_("&Question (Enter sends, Shift+Enter starts a new line):"),
			wx.TextCtrl,
			style=wx.TE_MULTILINE,
			size=(500, 80),
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
			size=(500, 250),
		)
		if state.transcript:
			self.conversationCtrl.SetValue("\n\n".join(state.transcript))
			self.conversationCtrl.SetInsertionPointEnd()

		bottomButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: Button that copies the last answer to the clipboard.
		copyButton = bottomButtons.addButton(self, label=_("C&opy last answer"))
		copyButton.Bind(wx.EVT_BUTTON, self.onCopy)
		# Translators: Button that clears the conversation.
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
		self.questionCtrl.SetFocus()

	# Keyboard

	def onCharHook(self, evt):
		key = evt.GetKeyCode()
		if key == wx.WXK_ESCAPE:
			if self._busy:
				self.cancelRequest(announce=True)
			else:
				self.Close()
			return
		if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and self.FindFocus() == self.questionCtrl:
			if evt.ShiftDown():
				self.questionCtrl.WriteText("\n")
			elif not evt.AltDown():
				self.ask()
			return
		evt.Skip()

	# Asking

	def onAskButton(self, evt):
		if self._busy:
			self.cancelRequest(announce=True)
		else:
			self.ask()

	def ask(self):
		if self._busy:
			# Translators: Reported when the user tries to send while an answer is pending.
			ui.message(_("Still waiting for the answer. Press Escape to cancel."))
			return
		question = self.questionCtrl.GetValue().strip()
		if not question:
			# Translators: Reported when the user sends an empty question.
			ui.message(_("Type or dictate a question first."))
			self.questionCtrl.SetFocus()
			return
		conf = getConf()
		history = list(state.history) if conf["keepContext"] else []
		prompt = codexClient.buildPrompt(question, history, conf["instructions"])
		client = makeClient()
		# Translators: Speaker name for the user's lines in the conversation.
		self._appendTranscript(formatTurn(_("You"), question))
		self._pendingQuestion = question
		self.questionCtrl.Clear()
		self._requestId += 1
		requestId = self._requestId
		cancelEvent = threading.Event()
		self._cancelEvent = cancelEvent
		self._setBusy(True)
		# Translators: Announced while waiting for an answer.
		ui.message(_("Asking GPT…"))
		threading.Thread(
			target=self._askWorker,
			args=(client, prompt, cancelEvent, requestId, question),
			name="askGPT.ask",
			daemon=True,
		).start()

	def _askWorker(self, client, prompt, cancelEvent, requestId, question):
		try:
			answer = client.ask(prompt, cancelEvent)
		except CodexError as e:
			if e.kind not in (codexClient.KIND_CANCELLED,):
				log.debugWarning("Ask GPT: %s (%s) %s" % (e.kind, e.message, e.detail))
			wx.CallAfter(self._onFailure, requestId, question, e)
		except Exception as e:
			log.error("Ask GPT: unexpected error", exc_info=True)
			wx.CallAfter(self._onFailure, requestId, question, CodexError(codexClient.KIND_OTHER, str(e)))
		else:
			wx.CallAfter(self._onAnswer, requestId, question, answer)

	def _onAnswer(self, requestId, question, answer):
		plain = stripMarkdown(answer) or answer
		if not self or requestId != self._requestId:
			return
		self._setBusy(False)
		state.addExchange(question, answer)
		state.lastAnswer = plain
		# Translators: Speaker name for GPT's lines in the conversation.
		self._appendTranscript(formatTurn(_("GPT"), plain), moveCaretToStart=True)
		if getConf()["speakAnswers"]:
			ui.message(plain)
		else:
			# Translators: Announced when an answer arrives and automatic speaking is off.
			ui.message(_("Answer received. It is in the conversation box."))

	def _onFailure(self, requestId, question, error):
		if not self or requestId != self._requestId:
			return
		self._setBusy(False)
		message = friendlyError(error)
		# Translators: Prefix for error lines in the conversation.
		self._appendTranscript(formatTurn(_("Error"), message))
		if not self.questionCtrl.GetValue().strip():
			self.questionCtrl.SetValue(question)
			self.questionCtrl.SetInsertionPointEnd()
		if error.kind == codexClient.KIND_CANCELLED:
			ui.message(message)
			return
		if error.kind in (codexClient.KIND_NOT_LOGGED_IN, codexClient.KIND_NOT_INSTALLED):
			# Translators: Question offering to open the settings after an error.
			offer = message + "\n\n" + _("Open the Ask GPT settings now?")
			if askYesNo(offer, _("Ask GPT"), self):
				openSettings()
			return
		showError(message, self)

	def cancelRequest(self, announce=False):
		if not self._busy:
			return
		if self._cancelEvent is not None:
			self._cancelEvent.set()
		# Invalidate the pending request so a late reply is ignored.
		self._requestId += 1
		self._setBusy(False)
		if self._pendingQuestion and not self.questionCtrl.GetValue().strip():
			self.questionCtrl.SetValue(self._pendingQuestion)
			self.questionCtrl.SetInsertionPointEnd()
		if announce:
			# Translators: Announced when the user cancels a question.
			ui.message(_("Request cancelled."))
			self.questionCtrl.SetFocus()

	def _setBusy(self, busy):
		self._busy = busy
		if busy:
			# Translators: Label of the Ask button while waiting; pressing it cancels.
			self.askButton.SetLabel(_("&Cancel"))
			self.dictateButton.Disable()
			self.beepTimer.Start(BEEP_INTERVAL_MS)
		else:
			self.beepTimer.Stop()
			self.askButton.SetLabel(_("&Ask"))
			self.dictateButton.Enable()
			self._cancelEvent = None

	def onBeepTimer(self, evt):
		if self._busy:
			tones.beep(440, 40, left=20, right=20)

	# Conversation view

	def _appendTranscript(self, text, moveCaretToStart=False):
		state.transcript.append(text)
		ctrl = self.conversationCtrl
		if ctrl.GetLastPosition() > 0:
			ctrl.AppendText("\n\n")
		start = ctrl.GetLastPosition()
		ctrl.AppendText(text)
		if moveCaretToStart:
			# So that moving to the conversation lands on the beginning of the newest answer.
			ctrl.SetInsertionPoint(start)
			ctrl.ShowPosition(start)

	def onCopy(self, evt):
		if not state.lastAnswer:
			# Translators: Reported when there is no answer to copy.
			ui.message(_("There is no answer yet."))
			return
		if api.copyToClip(state.lastAnswer):
			# Translators: Reported after copying the answer.
			ui.message(_("Last answer copied to the clipboard."))
		else:
			# Translators: Reported when copying failed.
			ui.message(_("Could not copy to the clipboard."))

	def onNewConversation(self, evt):
		self.cancelRequest()
		state.clear()
		self.conversationCtrl.Clear()
		self.questionCtrl.Clear()
		self._pendingQuestion = ""
		# Translators: Announced when the conversation is cleared.
		ui.message(_("New conversation started."))
		self.questionCtrl.SetFocus()

	# Voice input

	def onDictate(self, evt):
		if self._busy:
			return
		self.questionCtrl.SetFocus()
		self._dictateToken += 1
		token = self._dictateToken
		# Translators: Announced before Windows voice typing opens.
		message = _("Voice typing. Speak your question, then press Enter to send.")

		def startVoiceTyping():
			if not self or token != self._dictateToken:
				return
			self._dictateToken += 1  # Only once, whichever trigger fires first.
			self.questionCtrl.SetFocus()
			# Let focus settle before opening voice typing.
			wx.CallLater(300, self._sendVoiceTypingKey)

		# Open voice typing only after the announcement, so the microphone does not hear NVDA.
		try:
			import braille
			import speech
			from speech.commands import CallbackCommand

			speech.speak([message, CallbackCommand(lambda: wx.CallAfter(startVoiceTyping), name="askGPT.dictate")])
			braille.handler.message(message)
			# Fallback in case speech is off and the callback never runs.
			wx.CallLater(6000, startVoiceTyping)
		except Exception:
			log.debugWarning("Ask GPT: speech callback unavailable", exc_info=True)
			ui.message(message)
			wx.CallLater(2000, startVoiceTyping)

	def _sendVoiceTypingKey(self):
		if not self:
			return
		try:
			import keyboardHandler

			keyboardHandler.KeyboardInputGesture.fromName("windows+h").send()
		except Exception:
			log.error("Ask GPT: could not send Windows+H", exc_info=True)
			# Translators: Reported when voice typing could not be started.
			ui.message(_("Could not start Windows voice typing. Press Windows+H yourself."))

	# Closing

	def onClose(self, evt):
		self.cancelRequest()
		self._dictateToken += 1
		self.beepTimer.Stop()
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


class AskGPTSettingsPanel(SettingsPanel):
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
		conf = getConf()
		self._statusGeneration = 0
		sHelper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		description = sHelper.addItem(wx.StaticText(self, label=self.panelDescription))
		description.Wrap(self.scaleSize(544))

		self.statusCtrl = sHelper.addLabeledControl(
			# Translators: Label of the read-only sign-in status field.
			_("Status:"),
			wx.TextCtrl,
			style=wx.TE_READONLY,
			size=(400, -1),
		)
		# Translators: Status shown before the check finishes.
		self.statusCtrl.SetValue(_("Checking…"))

		accountButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: Button that starts ChatGPT sign-in in the browser.
		signInButton = accountButtons.addButton(self, label=_("&Sign in with ChatGPT..."))
		signInButton.Bind(wx.EVT_BUTTON, self.onSignIn)
		# Translators: Button that checks the sign-in status.
		checkButton = accountButtons.addButton(self, label=_("C&heck status"))
		checkButton.Bind(wx.EVT_BUTTON, lambda evt: self.refreshStatus(announce=True))
		# Translators: Button that signs out of ChatGPT in Codex.
		signOutButton = accountButtons.addButton(self, label=_("Sign &out"))
		signOutButton.Bind(wx.EVT_BUTTON, self.onSignOut)
		sHelper.addItem(accountButtons)

		pathHelper = guiHelper.BoxSizerHelper(self, orientation=wx.HORIZONTAL)
		self.pathCtrl = pathHelper.addLabeledControl(
			# Translators: Label of the Codex program path field.
			_("Codex executable path (leave blank to detect automatically):"),
			wx.TextCtrl,
			size=(300, -1),
		)
		self.pathCtrl.SetValue(conf["codexPath"])
		# Translators: Button to choose the Codex program file.
		browseButton = pathHelper.addItem(wx.Button(self, label=_("&Browse...")))
		browseButton.Bind(wx.EVT_BUTTON, self.onBrowse)
		sHelper.addItem(pathHelper)

		self.modelCtrl = sHelper.addLabeledControl(
			# Translators: Label of the model field.
			_("&Model (leave blank to use your ChatGPT plan's default):"),
			wx.ComboBox,
			choices=["", "gpt-5", "gpt-5-codex"],
			style=wx.CB_DROPDOWN,
		)
		self.modelCtrl.SetValue(conf["model"])

		self._effortValues = list(codexClient.REASONING_EFFORTS)
		effortLabels = [
			# Translators: Reasoning effort choice.
			_("Low (fastest)"),
			# Translators: Reasoning effort choice.
			_("Medium"),
			# Translators: Reasoning effort choice.
			_("High (slowest, most thorough)"),
		]
		self.effortCtrl = sHelper.addLabeledControl(
			# Translators: Label of the reasoning effort choice.
			_("&Reasoning effort:"),
			wx.Choice,
			choices=effortLabels,
		)
		effort = conf["reasoningEffort"]
		self.effortCtrl.SetSelection(
			self._effortValues.index(effort) if effort in self._effortValues else 0,
		)

		self.instructionsCtrl = sHelper.addLabeledControl(
			# Translators: Label of the instructions (system prompt) field.
			_("&Instructions for GPT:"),
			wx.TextCtrl,
			style=wx.TE_MULTILINE,
			size=(400, 100),
		)
		self.instructionsCtrl.SetValue(conf["instructions"])

		self.speakCheck = sHelper.addItem(
			# Translators: Checkbox to speak answers automatically.
			wx.CheckBox(self, label=_("Speak &answers automatically")),
		)
		self.speakCheck.SetValue(conf["speakAnswers"])
		self.contextCheck = sHelper.addItem(
			# Translators: Checkbox to keep conversation context.
			wx.CheckBox(self, label=_("&Keep conversation context for follow-up questions")),
		)
		self.contextCheck.SetValue(conf["keepContext"])

		self.timeoutCtrl = sHelper.addLabeledControl(
			# Translators: Label of the timeout spin control.
			_("Answer &timeout in seconds:"),
			nvdaControls.SelectOnFocusSpinCtrl,
			min=codexClient.MIN_TIMEOUT,
			max=codexClient.MAX_TIMEOUT,
			initial=conf["timeout"],
		)

	def onPanelActivated(self):
		super().onPanelActivated()
		self.refreshStatus(announce=False)

	# Status and account

	def _setStatus(self, generation, text, announce):
		if not self or generation != self._statusGeneration:
			return
		self.statusCtrl.SetValue(text)
		if announce:
			ui.message(text)

	def refreshStatus(self, announce=False):
		self._statusGeneration += 1
		generation = self._statusGeneration
		self.statusCtrl.SetValue(_("Checking…"))
		client = makeClient(executablePath=self.pathCtrl.GetValue())

		def worker():
			try:
				loggedIn, text = client.loginStatus()
			except CodexError as e:
				if e.kind == codexClient.KIND_NOT_INSTALLED:
					# Translators: Status when the Codex program cannot be found.
					status = _("Codex CLI not found")
				else:
					# Translators: Status when the check failed. {error} is the reason.
					status = _("Could not check the status: {error}").format(error=friendlyError(e))
			except Exception as e:
				log.error("Ask GPT: status check failed", exc_info=True)
				status = _("Could not check the status: {error}").format(error=e)
			else:
				if loggedIn and codexClient.isApiKeyLogin(text):
					# Translators: Status when Codex uses an API key instead of a ChatGPT account.
					status = _("Signed in with an API key (billed per use, not to your ChatGPT plan)")
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
		cancelLogin()
		client = makeClient(executablePath=self.pathCtrl.GetValue())
		try:
			proc = client.login()
		except CodexError as e:
			showError(friendlyError(e), self)
			self.refreshStatus()
			return
		_loginClient = client
		self._statusGeneration += 1
		# Translators: Status while the user signs in in the browser.
		self.statusCtrl.SetValue(_("Waiting for you to sign in in your browser…"))
		ui.message(
			# Translators: Announced when sign-in starts.
			_(
				"Your browser will open. Sign in with your ChatGPT account, "
				"then come back and press Check status."
			),
		)

		def waiter():
			global _loginClient
			try:
				proc.wait(timeout=600)
			except Exception:
				client.cancelLogin()
			if _loginClient is not client:
				return  # Superseded by a newer sign-in attempt, sign-out or add-on shutdown.
			_loginClient = None
			try:
				loggedIn, text = client.loginStatus()
			except Exception:
				return
			wx.CallAfter(_announceLoginResult, loggedIn, text)
			wx.CallAfter(self._refreshIfAlive)

		threading.Thread(target=waiter, name="askGPT.login", daemon=True).start()

	def _refreshIfAlive(self):
		if self:
			self.refreshStatus()

	def onSignOut(self, evt):
		cancelLogin()
		self._statusGeneration += 1
		generation = self._statusGeneration
		client = makeClient(executablePath=self.pathCtrl.GetValue())

		def worker():
			try:
				ok, text = client.logout()
				# Translators: Status after signing out.
				status = _("Signed out") if ok else (text or _("Not signed in"))
			except CodexError as e:
				status = friendlyError(e)
			wx.CallAfter(self._setStatus, generation, status, True)

		threading.Thread(target=worker, name="askGPT.logout", daemon=True).start()

	def onBrowse(self, evt):
		with wx.FileDialog(
			self,
			# Translators: Title of the file chooser for the Codex program.
			_("Select the Codex program"),
			defaultDir=os.path.dirname(self.pathCtrl.GetValue()),
			# Translators: File type filter in the file chooser.
			wildcard=_("Programs (*.exe;*.cmd)|*.exe;*.cmd|All files (*.*)|*.*"),
			style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
		) as fileDialog:
			if fileDialog.ShowModal() == wx.ID_OK:
				self.pathCtrl.SetValue(fileDialog.GetPath())
				self.refreshStatus(announce=True)

	# Saving

	def isValid(self):
		path = self.pathCtrl.GetValue().strip().strip('"')
		if path and not os.path.isfile(path):
			# Translators: Error when the Codex path does not exist.
			showError(_("The Codex executable path does not exist: {path}").format(path=path), self)
			self.pathCtrl.SetFocus()
			return False
		model = self.modelCtrl.GetValue().strip()
		if not codexClient.isValidModel(model):
			showError(
				# Translators: Error when the model name has invalid characters.
				_("The model name may only contain letters, digits, dots, dashes, colons and slashes."),
				self,
			)
			self.modelCtrl.SetFocus()
			return False
		return super().isValid()

	def onSave(self):
		conf = getConf()
		conf["codexPath"] = self.pathCtrl.GetValue().strip().strip('"')
		conf["model"] = self.modelCtrl.GetValue().strip()
		selection = self.effortCtrl.GetSelection()
		conf["reasoningEffort"] = self._effortValues[selection] if selection >= 0 else "low"
		instructions = self.instructionsCtrl.GetValue().strip()
		conf["instructions"] = instructions or codexClient.DEFAULT_INSTRUCTIONS
		conf["speakAnswers"] = self.speakCheck.GetValue()
		conf["keepContext"] = self.contextCheck.GetValue()
		conf["timeout"] = codexClient.clampTimeout(self.timeoutCtrl.GetValue())
