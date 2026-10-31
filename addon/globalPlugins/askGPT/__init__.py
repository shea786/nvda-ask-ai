# -*- coding: UTF-8 -*-
# Ask GPT NVDA add-on: ask ChatGPT questions through the OpenAI Codex CLI,
# signed in with a ChatGPT account, and hear the answers.

import threading

import wx

import addonHandler
import config
import globalPluginHandler
import globalVars
import gui
import scriptHandler
import ui
from gui import settingsDialogs
from logHandler import log
from scriptHandler import script

from . import codexClient, dialogs, dictation

addonHandler.initTranslation()

confspec = {
	"codexPath": 'string(default="")',
	"model": 'string(default="")',
	# Free text: models offer different levels (low ... xhigh, max); validated before use.
	"reasoningEffort": 'string(default="low")',
	"instructions": 'string(default="%s")' % codexClient.DEFAULT_INSTRUCTIONS,
	"speakAnswers": "boolean(default=True)",
	"keepContext": "boolean(default=True)",
	"beepWhileWaiting": "boolean(default=True)",
	"allowProjectWrites": "boolean(default=True)",
	# Blank means %LOCALAPPDATA%\\askGPT\\conversations.
	"conversationsDir": 'string(default="")',
	"dictationSilenceTimeout": "integer(default=%d, min=%d, max=%d)"
	% (int(dictation.DEFAULT_SILENCE_TIMEOUT), dictation.MIN_SILENCE_TIMEOUT, dictation.MAX_SILENCE_TIMEOUT),
	"timeout": "integer(default=%d, min=%d, max=%d)"
	% (codexClient.DEFAULT_TIMEOUT, codexClient.MIN_TIMEOUT, codexClient.MAX_TIMEOUT),
}
config.conf.spec[dialogs.CONFIG_SECTION] = confspec

# Translators: Category of the Ask GPT commands in the Input Gestures dialog.
SCRIPT_CATEGORY = _("Ask GPT")


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	scriptCategory = SCRIPT_CATEGORY

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.toolsMenuItem = None
		self.conversationsMenuItem = None
		if globalVars.appArgs.secure:
			# Never run on secure screens (sign-in screen, UAC prompts).
			return
		# Outside NVDA's configuration folder, so it is never copied or synced with the settings.
		dialogs.workDir = codexClient.defaultWorkDir()
		threading.Thread(target=self._cleanWorkDir, name="askGPT.cleanup", daemon=True).start()
		settingsDialogs.NVDASettingsDialog.categoryClasses.append(dialogs.AskGPTSettingsPanel)
		toolsMenu = gui.mainFrame.sysTrayIcon.toolsMenu
		self.toolsMenuItem = toolsMenu.Append(
			wx.ID_ANY,
			# Translators: Item in the NVDA Tools menu.
			_("Ask &GPT..."),
			# Translators: Help text of the Tools menu item.
			_("Ask ChatGPT a question and hear the answer"),
		)
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self.onToolsMenu, self.toolsMenuItem)
		self.conversationsMenuItem = toolsMenu.Append(
			wx.ID_ANY,
			# Translators: Item in the NVDA Tools menu that lists saved Ask GPT conversations.
			_("Ask GPT con&versations..."),
			# Translators: Help text of the conversations menu item.
			_("Continue, rename, archive, export or delete saved Ask GPT conversations"),
		)
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self.onConversationsMenu, self.conversationsMenuItem)

	@staticmethod
	def _cleanWorkDir():
		try:
			codexClient.cleanStaleAnswerFiles(dialogs.workDir)
		except Exception:
			log.debugWarning("Ask GPT: could not clean the working folder", exc_info=True)

	def onToolsMenu(self, evt):
		wx.CallAfter(dialogs.openAskDialog)

	def onConversationsMenu(self, evt):
		wx.CallAfter(dialogs.openConversationManager)

	@script(
		# Translators: Description of the command that opens the Ask GPT dialog.
		description=_("Opens the Ask GPT dialog to ask ChatGPT a question"),
		category=SCRIPT_CATEGORY,
		gesture="kb:NVDA+alt+g",
	)
	def script_askGPT(self, gesture):
		if globalVars.appArgs.secure:
			return
		wx.CallAfter(dialogs.openAskDialog)

	@script(
		# Translators: Description of the command that opens the list of saved conversations.
		description=_("Show Ask GPT conversations"),
		category=SCRIPT_CATEGORY,
	)
	def script_showConversations(self, gesture):
		if globalVars.appArgs.secure:
			return
		wx.CallAfter(dialogs.openConversationManager)

	@script(
		# Translators: Description of the command that repeats the last answer.
		description=_("Speaks the last answer from GPT. Press twice to show it in a window you can review"),
		category=SCRIPT_CATEGORY,
		gesture="kb:NVDA+alt+shift+g",
		speakOnDemand=True,
	)
	def script_speakLastAnswer(self, gesture):
		if globalVars.appArgs.secure:
			return
		try:
			# The current conversation, or after a restart the most recently used saved one.
			dialogs.state.ensureCurrent()
		except Exception:
			log.debugWarning("Ask GPT: could not load the current conversation", exc_info=True)
		if not dialogs.state.lastAnswer:
			# Translators: Reported when no answer has been received yet.
			ui.message(_("No answer from GPT yet."))
			return
		if scriptHandler.getLastScriptRepeatCount() >= 1:
			# The window shows the answer exactly as GPT wrote it, including code.
			# Translators: Title of the window showing the last answer.
			ui.browseableMessage(dialogs.state.lastAnswer, _("Last GPT answer"))
		else:
			ui.message(dialogs.state.lastAnswerSpoken)

	def terminate(self):
		try:
			# Kills a running question and sign-in synchronously, then closes the dialog.
			dialogs.shutdown()
		except Exception:
			log.debugWarning("Ask GPT: error during shutdown", exc_info=True)
		try:
			settingsDialogs.NVDASettingsDialog.categoryClasses.remove(dialogs.AskGPTSettingsPanel)
		except ValueError:
			pass
		for item in (self.toolsMenuItem, self.conversationsMenuItem):
			if item is not None:
				try:
					gui.mainFrame.sysTrayIcon.toolsMenu.Remove(item)
				except Exception:
					log.debugWarning("Ask GPT: error removing menu item", exc_info=True)
		self.toolsMenuItem = None
		self.conversationsMenuItem = None
		super().terminate()
