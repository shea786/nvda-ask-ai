# -*- coding: UTF-8 -*-
# Ask GPT NVDA add-on: the Ask dialog, the settings panel and the request lifecycle.

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
from gui.settingsDialogs import NVDASettingsDialog, SettingsPanel
from logHandler import log

from . import codexClient
from .codexClient import CodexClient, CodexError
from .textUtils import estimatedSpeechMs, formatTurn, stripMarkdown

addonHandler.initTranslation()

CONFIG_SECTION = "askGPT"
# Set by the global plugin (see codexClient.defaultWorkDir).
workDir = None
# Keep at most this many messages (questions plus answers) for context.
MAX_STORED_MESSAGES = 100
# Keep at most this many entries in the Conversation box.
MAX_TRANSCRIPT_ENTRIES = 100
BEEP_INTERVAL_MS = 2000
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
# Read-only multi-line, so that Tab reaches it (see AskGPTSettingsPanel.makeSettings).
STATUS_STYLE = wx.TE_READONLY | wx.TE_MULTILINE | wx.TE_NO_VSCROLL


def _once(func):
	"""Wrap func so that it runs at most once, whichever trigger fires first."""
	done = []

	def wrapper(*args):
		if not done:
			done.append(True)
			func(*args)

	return wrapper


class PendingRequest:
	"""A question being answered. Owned by the module, so it survives closing the dialog."""

	def __init__(self, client, question):
		self.client = client
		self.question = question
		self.cancelEvent = threading.Event()


class ConversationState:
	"""Conversation shared between dialog instances, so closing the dialog keeps the context."""

	def __init__(self):
		self.history = []  # (role, raw text), role is "user" or "assistant".
		self.transcript = []  # Entries shown in the Conversation box (Markdown stripped).
		self.lastAnswer = ""  # Raw answer text, for copying and the browseable window.
		self.lastAnswerIndex = None  # Index in transcript of the newest answer.
		self.pending = None

	def clear(self):
		self.history = []
		self.transcript = []
		self.lastAnswer = ""
		self.lastAnswerIndex = None

	@property
	def lastAnswerSpoken(self):
		return stripMarkdown(self.lastAnswer) or self.lastAnswer

	def addExchange(self, question, answer):
		self.history.append(("user", question))
		self.history.append(("assistant", answer))
		del self.history[:-MAX_STORED_MESSAGES]

	def appendTranscript(self, text, isAnswer=False):
		"""Add an entry, dropping the oldest beyond MAX_TRANSCRIPT_ENTRIES. Returns how many were dropped."""
		self.transcript.append(text)
		if isAnswer:
			self.lastAnswerIndex = len(self.transcript) - 1
		dropped = max(0, len(self.transcript) - MAX_TRANSCRIPT_ENTRIES)
		if dropped:
			del self.transcript[:dropped]
			if self.lastAnswerIndex is not None:
				self.lastAnswerIndex -= dropped
				if self.lastAnswerIndex < 0:
					self.lastAnswerIndex = None
		return dropped


state = ConversationState()
# The client running `codex login`. Only read and written on the main thread.
_loginClient = None
# The open settings panel, if any, so background results can refresh it.
_activePanel = None


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
	if detail and getattr(error, "reason", "") != codexClient.REASON_CUSTOM_PATH:
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


# Request lifecycle (main thread unless noted)


def startRequest(question):
	conf = getConf()
	history = list(state.history) if conf["keepContext"] else []
	prompt = codexClient.buildPrompt(question, history, conf["instructions"])
	pending = PendingRequest(makeClient(), question)
	state.pending = pending
	threading.Thread(target=_requestWorker, args=(pending, prompt), name="askGPT.ask", daemon=True).start()
	return pending


def _requestWorker(pending, prompt):
	"""Background thread."""
	try:
		answer = pending.client.ask(prompt, pending.cancelEvent)
	except CodexError as e:
		if e.kind != codexClient.KIND_CANCELLED:
			log.debugWarning("Ask GPT: %s: %s | %s" % (e.kind, e.message, e.detail))
		wx.CallAfter(_onRequestDone, pending, None, e)
	except Exception as e:
		log.error("Ask GPT: unexpected error", exc_info=True)
		error = CodexError(codexClient.KIND_OTHER, "Something went wrong while asking GPT.", str(e))
		wx.CallAfter(_onRequestDone, pending, None, error)
	else:
		wx.CallAfter(_onRequestDone, pending, answer, None)


def cancelRequest():
	"""Cancel the running request, if any. Returns it, or None."""
	pending = state.pending
	if pending is None:
		return None
	state.pending = None
	pending.cancelEvent.set()
	return pending


def _onRequestDone(pending, answer, error):
	if pending is not state.pending:
		return  # Cancelled, or replaced by a newer conversation.
	state.pending = None
	dialog = AskGPTDialog.getInstance()
	if error is None:
		state.addExchange(pending.question, answer)
		state.lastAnswer = answer
		spoken = state.lastAnswerSpoken
		# Translators: Speaker name for GPT's lines in the conversation.
		dropped = state.appendTranscript(formatTurn(_("GPT"), spoken), isAnswer=True)
		if dialog is not None:
			dialog.onAnswer(spoken, dropped)
		elif getConf()["speakAnswers"]:
			ui.message(spoken)
		else:
			# Translators: Announced when an answer arrives after the dialog was closed and speaking is off.
			ui.message(_("GPT's answer is ready. Press NVDA+Alt+Shift+G to hear it."))
		return
	# Translators: Prefix for error lines in the conversation.
	dropped = state.appendTranscript(formatTurn(_("Error"), transcriptErrorText(error)))
	if dialog is not None:
		dialog.onFailure(pending.question, error, dropped)
	else:
		ui.message(friendlyError(error))


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


def shutdown():
	"""Kill everything synchronously. Called from GlobalPlugin.terminate()."""
	pending = cancelRequest()
	if pending is not None:
		pending.client.terminate()
	cancelLogin()
	closeAskDialog()


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
		self._busy = False
		self._dictateToken = 0
		self._lastAnswerPos = None
		self.beepTimer = wx.Timer(self)

		mainSizer = wx.BoxSizer(wx.VERTICAL)
		sHelper = guiHelper.BoxSizerHelper(self, orientation=wx.VERTICAL)

		self.questionCtrl = sHelper.addLabeledControl(
			# Translators: Label of the box where the question is typed.
			_("&Question:"),
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
		self._fillConversation()
		if self._lastAnswerPos is not None:
			# Reopening lands on the beginning of the newest answer.
			self.conversationCtrl.SetInsertionPoint(self._lastAnswerPos)
			self.conversationCtrl.ShowPosition(self._lastAnswerPos)

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
		if state.pending is not None:
			self._setBusy(True)  # A question asked before the dialog was closed is still running.
		self.questionCtrl.SetFocus()

	# Keyboard

	def onCharHook(self, evt):
		key = evt.GetKeyCode()
		if key == wx.WXK_ESCAPE:
			if state.pending is not None:
				self.cancel(announce=True)
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

	def _announceAfterFocus(self, control, message):
		control.SetFocus()
		wx.CallLater(ANNOUNCE_AFTER_FOCUS_MS, ui.message, message)

	# Asking

	def onAskButton(self, evt):
		if state.pending is not None:
			self.cancel(announce=True)
		else:
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
		startRequest(question)
		# Translators: Speaker name for the user's lines in the conversation.
		entry = formatTurn(_("You"), question)
		self._showEntry(entry, state.appendTranscript(entry))
		self.questionCtrl.Clear()
		self._setBusy(True)
		# Translators: Announced while waiting for an answer.
		ui.message(_("Asking GPT…"))

	def onAnswer(self, spoken, dropped):
		self._setBusy(False)
		self._showEntry(state.transcript[-1], dropped, isAnswer=True)
		if getConf()["speakAnswers"]:
			ui.message(spoken)
		else:
			# Translators: Announced when an answer arrives and automatic speaking is off.
			ui.message(_("Answer received. It is in the conversation box."))

	def onFailure(self, question, error, dropped):
		self._setBusy(False)
		self._showEntry(state.transcript[-1], dropped)
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

	# Conversation view

	def _fillConversation(self):
		"""Rebuild the Conversation box from state, remembering where the newest answer starts."""
		ctrl = self.conversationCtrl
		ctrl.Clear()
		self._lastAnswerPos = None
		for index, entry in enumerate(state.transcript):
			if index:
				ctrl.AppendText("\n\n")
			if index == state.lastAnswerIndex:
				self._lastAnswerPos = ctrl.GetLastPosition()
			ctrl.AppendText(entry)

	def _showEntry(self, text, dropped, isAnswer=False):
		"""Mirror an entry already added to state.transcript into the Conversation box."""
		ctrl = self.conversationCtrl
		hasFocus = self.FindFocus() == ctrl
		if dropped:
			# Old entries were dropped from the start; rebuild (rare: after MAX_TRANSCRIPT_ENTRIES entries).
			caret = ctrl.GetInsertionPoint()
			self._fillConversation()
			if hasFocus:
				ctrl.SetInsertionPoint(min(caret, ctrl.GetLastPosition()))
				return
		else:
			if ctrl.GetLastPosition() > 0:
				ctrl.AppendText("\n\n")
			start = ctrl.GetLastPosition()
			ctrl.AppendText(text)
			if isAnswer:
				self._lastAnswerPos = start
		if isAnswer and not hasFocus and self._lastAnswerPos is not None:
			# Moving to the conversation then lands on the newest answer; never move a caret the user is using.
			ctrl.SetInsertionPoint(self._lastAnswerPos)
			ctrl.ShowPosition(self._lastAnswerPos)

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
		if state.transcript or state.pending is not None:
			# Translators: Confirmation before clearing the conversation.
			if not askYesNo(_("Clear the current conversation and start a new one?"), _("Ask GPT"), self):
				return
		cancelRequest()
		self._setBusy(False)
		state.clear()
		self.conversationCtrl.Clear()
		self._lastAnswerPos = None
		self.questionCtrl.Clear()
		# Translators: Announced when the conversation is cleared.
		self._announceAfterFocus(self.questionCtrl, _("New conversation started."))

	# Voice input

	def onDictate(self, evt):
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
			wx.CallLater(300, self._sendVoiceTypingKey)

		# Open voice typing only after the announcement, so the microphone does not hear NVDA.
		speakThen(message, startVoiceTyping)

	def _sendVoiceTypingKey(self):
		if not self:
			return
		if not self.IsActive() or self.FindFocus() != self.questionCtrl:
			# Translators: Reported when voice typing was not started because focus moved.
			ui.message(_("Voice typing was not started because the focus left the question box."))
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
		if state.pending is not None:
			if getConf()["speakAnswers"]:
				# Translators: Announced when the dialog is closed while waiting for an answer.
				ui.message(_("GPT will speak the answer when it's ready."))
			else:
				# Translators: Announced when the dialog is closed while waiting and speaking is off.
				ui.message(_("GPT is still working. You will hear when the answer is ready."))
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
	# Mnemonics used here: S, H, U, Y, X, B, M, R, I, P, K, W, T.
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
		pathLabel = wx.StaticText(
			self,
			# Translators: Label of the Codex program path field.
			label=_("Codex e&xecutable path (leave blank to detect automatically):"),
		)
		self.pathCtrl = wx.TextCtrl(self, size=(self.scaleSize(PATH_MIN_WIDTH), -1))
		self.pathCtrl.SetValue(conf["codexPath"])
		# Translators: Button to choose the Codex program file.
		browseButton = wx.Button(self, label=_("&Browse..."))
		browseButton.Bind(wx.EVT_BUTTON, self.onBrowse)
		pathRow = wx.BoxSizer(wx.HORIZONTAL)
		pathRow.Add(self.pathCtrl, proportion=1, flag=wx.ALIGN_CENTER_VERTICAL)
		pathRow.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_HORIZONTAL)
		pathRow.Add(browseButton, flag=wx.ALIGN_CENTER_VERTICAL)
		pathBlock = wx.BoxSizer(wx.VERTICAL)
		pathBlock.Add(pathLabel)
		pathBlock.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_VERTICAL)
		pathBlock.Add(pathRow, flag=wx.EXPAND)
		sHelper.addItem(pathBlock, flag=wx.EXPAND)

		self.modelCtrl = sHelper.addLabeledControl(
			# Translators: Label of the model field.
			_("&Model (blank uses your plan's default):"),
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

		self.timeoutCtrl = sHelper.addLabeledControl(
			# Translators: Label of the timeout spin control.
			_("Answer &timeout in seconds:"),
			nvdaControls.SelectOnFocusSpinCtrl,
			min=codexClient.MIN_TIMEOUT,
			max=codexClient.MAX_TIMEOUT,
			initial=conf["timeout"],
		)

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

	# Saving

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
		conf["codexPath"] = self.pathCtrl.GetValue().strip()
		conf["model"] = self.modelCtrl.GetValue().strip()
		selection = self.effortCtrl.GetSelection()
		conf["reasoningEffort"] = self._effortValues[selection] if selection >= 0 else "low"
		instructions = self.instructionsCtrl.GetValue().strip()
		conf["instructions"] = instructions or codexClient.DEFAULT_INSTRUCTIONS
		conf["speakAnswers"] = self.speakCheck.GetValue()
		conf["keepContext"] = self.contextCheck.GetValue()
		conf["beepWhileWaiting"] = self.beepCheck.GetValue()
		conf["timeout"] = codexClient.clampTimeout(self.timeoutCtrl.GetValue())


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
