# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: ask ChatGPT (through the OpenAI Codex CLI) or Claude (through Claude Code),
# signed in with the user's own subscription, and hear the answers.

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

from . import attachments, context, conversations, dialogs, dictation, migration, providers, updateCheck
from .providers import base as providerBase

addonHandler.initTranslation()

confspec = {
	# The service new conversations use: "codex" (ChatGPT) or "claude".
	"provider": 'string(default="%s")' % providers.DEFAULT_PROVIDER,
	"codexPath": 'string(default="")',
	"model": 'string(default="")',
	# Free text: models offer different levels (low ... xhigh, max); validated before use.
	"reasoningEffort": 'string(default="%s")' % providerBase.DEFAULT_REASONING_EFFORT,
	"claudePath": 'string(default="")',
	"claudeModel": 'string(default="")',
	"claudeReasoningEffort": 'string(default="%s")' % providerBase.DEFAULT_REASONING_EFFORT,
	"instructions": 'string(default="%s")' % providerBase.DEFAULT_INSTRUCTIONS,
	"speakAnswers": "boolean(default=True)",
	"keepContext": "boolean(default=True)",
	"beepWhileWaiting": "boolean(default=True)",
	"allowProjectWrites": "boolean(default=True)",
	# Blank means %LOCALAPPDATA%\\askAI\\conversations.
	"conversationsDir": 'string(default="")',
	"dictationSilenceTimeout": "integer(default=%d, min=%d, max=%d)"
	% (int(dictation.DEFAULT_SILENCE_TIMEOUT), dictation.MIN_SILENCE_TIMEOUT, dictation.MAX_SILENCE_TIMEOUT),
	"timeout": "integer(default=%d, min=%d, max=%d)"
	% (providerBase.DEFAULT_TIMEOUT, providerBase.MIN_TIMEOUT, providerBase.MAX_TIMEOUT),
	# Say "Done: <title>." / "Failed: <title>. <reason>" when a question finishes while another app is in front.
	"announceInOtherApps": "boolean(default=True)",
	"autoUpdateCheck": "boolean(default=True)",
	"lastUpdateCheck": "float(default=0.0)",
	# Set once the Ask GPT 1.x settings and conversations were copied (or there were none).
	"migratedFromAskGPT": "boolean(default=False)",
	# Set once the user answered the question about removing the old Ask GPT add-on.
	"askedToRemoveAskGPT": "boolean(default=False)",
}
config.conf.spec[dialogs.CONFIG_SECTION] = confspec

# Typed defaults of the settings Ask GPT 1.x had, for converting its nvda.ini values.
MIGRATION_DEFAULTS = {
	"codexPath": "",
	"model": "",
	"reasoningEffort": providerBase.DEFAULT_REASONING_EFFORT,
	"instructions": providerBase.DEFAULT_INSTRUCTIONS,
	"speakAnswers": True,
	"keepContext": True,
	"beepWhileWaiting": True,
	"allowProjectWrites": True,
	"conversationsDir": "",
	"dictationSilenceTimeout": int(dictation.DEFAULT_SILENCE_TIMEOUT),
	"timeout": providerBase.DEFAULT_TIMEOUT,
}
# Delay before asking about the old add-on, so NVDA's own startup speech and dialogs come first.
OLD_ADDON_PROMPT_DELAY_MS = 8000

# Translators: Category of the Ask AI commands in the Input Gestures dialog.
SCRIPT_CATEGORY = _("Ask AI")


def migrateFromAskGPT():
	"""Copy Ask GPT 1.x settings (when Ask AI still has its defaults) and conversations (when the new
	folder is empty). Runs once; the old files and settings are left in place.
	"""
	conf = dialogs.getConf()
	if conf["migratedFromAskGPT"]:
		return
	try:
		baseProfile = config.conf.profiles[0]
		oldSection = baseProfile.get(migration.OLD_CONFIG_SECTION)
		newSection = baseProfile.get(dialogs.CONFIG_SECTION)
		# Only when Ask AI has no settings of its own yet ("has defaults").
		if oldSection and not newSection:
			for key, value in migration.migratedSettings(dict(oldSection), MIGRATION_DEFAULTS).items():
				conf[key] = value
				log.info("Ask AI: copied the %s setting from Ask GPT" % key)
	except Exception:
		log.error("Ask AI: could not copy the Ask GPT settings", exc_info=True)
	try:
		if not conf["conversationsDir"]:
			copied = migration.copyConversations(migration.oldConversationsDir(), conversations.defaultConversationsDir())
			if copied:
				log.info("Ask AI: copied %d conversations from Ask GPT" % copied)
	except Exception:
		log.error("Ask AI: could not copy the Ask GPT conversations", exc_info=True)
	conf["migratedFromAskGPT"] = True


def offerToRemoveOldAddon():
	"""Main thread. If Ask GPT 1.x is still installed and enabled, it would also take NVDA+Alt+G:
	ask once whether to remove it. The question is never repeated after either answer.
	"""
	if globalVars.appArgs.secure:
		return
	conf = dialogs.getConf()
	if conf["askedToRemoveAskGPT"]:
		return
	try:
		oldAddon = migration.findOldAddon(addonHandler.getAvailableAddons())
	except Exception:
		log.debugWarning("Ask AI: could not list add-ons", exc_info=True)
		return
	if oldAddon is None:
		return
	conf["askedToRemoveAskGPT"] = True
	gui.mainFrame.prePopup()
	try:
		remove = dialogs.askYesNo(
			# Translators: Asked once when the old Ask GPT add-on is still installed next to Ask AI.
			_("The old Ask GPT add-on is still installed. Remove it now? (Restart NVDA afterwards.)"),
			# Translators: Title of the question about removing the old add-on.
			_("Ask AI"),
			gui.mainFrame,
		)
	finally:
		gui.mainFrame.postPopup()
	if not remove:
		return
	try:
		# Addon.requestRemove() (NVDA 2024.1 to 2026.x) marks it for removal at the next restart.
		oldAddon.requestRemove()
	except Exception:
		log.error("Ask AI: could not remove the Ask GPT add-on", exc_info=True)
		# Translators: Error when the old add-on could not be marked for removal.
		dialogs.showError(_("Ask GPT could not be removed. Remove it in NVDA menu, Tools, Add-on Store, Installed add-ons."))
		return
	try:
		from gui import addonGui

		wx.CallAfter(addonGui.promptUserForRestart)
	except Exception:
		# Translators: Announced after the old add-on was marked for removal.
		ui.message(_("Ask GPT will be removed when NVDA restarts."))


def captureScreenContext():
	"""Selected text of the focus, else the navigator object's text, with the app and window names and a
	screenshot of the foreground window (only sent if the user ticks "Include a screenshot of the window").
	@raise context.ContextError: for protected fields or when there is no text.
	"""
	import api
	import controlTypes
	import textInfos

	focus = api.getFocusObject()
	navigator = api.getNavigatorObject()

	def selection(obj):
		return obj.makeTextInfo(textInfos.POSITION_SELECTION).text

	def allText(obj):
		info = obj.makeTextInfo(textInfos.POSITION_ALL)
		return info.text

	def protected(obj):
		return context.isProtectedObject(
			obj,
			passwordRole=getattr(controlTypes.Role, "PASSWORDEDIT", None),
			protectedState=getattr(controlTypes.State, "PROTECTED", None),
		)

	appName = ""
	try:
		appModule = focus.appModule
		appName = getattr(appModule, "productName", "") or appModule.appName
	except Exception:
		appName = ""
	windowTitle = ""
	try:
		windowTitle = api.getForegroundObject().name or ""
	except Exception:
		windowTitle = ""
	captured = context.captureContext(focus, navigator, selection, allText, appName, windowTitle, protected)
	try:
		from . import screenshot

		captured.screenshot = screenshot.captureWindow()
	except Exception:
		log.debugWarning("Ask AI: could not capture a screenshot", exc_info=True)
		captured.screenshot = None
	return captured


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	scriptCategory = SCRIPT_CATEGORY

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.toolsMenuItem = None
		self.conversationsMenuItem = None
		self.updatesMenuItem = None
		self._oldAddonTimer = None
		if globalVars.appArgs.secure:
			# Never run on secure screens (sign-in screen, UAC prompts).
			return
		try:
			migrateFromAskGPT()
		except Exception:
			log.error("Ask AI: migration from Ask GPT failed", exc_info=True)
		# Outside NVDA's configuration folder, so it is never copied or synced with the settings.
		dialogs.workDir = providerBase.defaultWorkDir()
		threading.Thread(target=self._cleanWorkDir, name="askAI.cleanup", daemon=True).start()
		settingsDialogs.NVDASettingsDialog.categoryClasses.append(dialogs.AskAISettingsPanel)
		toolsMenu = gui.mainFrame.sysTrayIcon.toolsMenu
		self.toolsMenuItem = toolsMenu.Append(
			wx.ID_ANY,
			# Translators: Item in the NVDA Tools menu (NVDA uses A for the Add-on Store there).
			_("As&k AI..."),
			# Translators: Help text of the Tools menu item.
			_("Ask ChatGPT or Claude a question and hear the answer"),
		)
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self.onToolsMenu, self.toolsMenuItem)
		self.conversationsMenuItem = toolsMenu.Append(
			wx.ID_ANY,
			# Translators: Item in the NVDA Tools menu that lists saved Ask AI conversations.
			_("Ask AI con&versations..."),
			# Translators: Help text of the conversations menu item.
			_("Continue, rename, archive, export or delete saved Ask AI conversations"),
		)
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self.onConversationsMenu, self.conversationsMenuItem)
		self.updatesMenuItem = toolsMenu.Append(
			wx.ID_ANY,
			# Translators: Item in the NVDA Tools menu that checks for a newer Ask AI version.
			_("Check for Ask AI &updates..."),
			# Translators: Help text of the update menu item.
			_("Check GitHub for a newer version of the Ask AI add-on"),
		)
		gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self.onUpdatesMenu, self.updatesMenuItem)
		try:
			updateCheck.scheduleAutomaticCheck()
		except Exception:
			log.debugWarning("Ask AI: could not schedule the update check", exc_info=True)
		try:
			self._oldAddonTimer = wx.CallLater(OLD_ADDON_PROMPT_DELAY_MS, self._offerToRemoveOldAddon)
		except Exception:
			log.debugWarning("Ask AI: could not schedule the old add-on check", exc_info=True)

	@staticmethod
	def _cleanWorkDir():
		try:
			providerBase.cleanStaleAnswerFiles(dialogs.workDir)
			attachments.RequestFiles.cleanStale(dialogs.workDir)
		except Exception:
			log.debugWarning("Ask AI: could not clean the working folder", exc_info=True)

	def _offerToRemoveOldAddon(self):
		self._oldAddonTimer = None
		try:
			offerToRemoveOldAddon()
		except Exception:
			log.error("Ask AI: could not check for the old Ask GPT add-on", exc_info=True)

	def onToolsMenu(self, evt):
		wx.CallAfter(dialogs.openAskDialog)

	def onConversationsMenu(self, evt):
		wx.CallAfter(dialogs.openConversationManager)

	def onUpdatesMenu(self, evt):
		wx.CallAfter(updateCheck.checkForUpdates, None)

	@script(
		# Translators: Description of the command that opens the Ask AI dialog.
		description=_("Opens the Ask AI dialog to ask ChatGPT or Claude a question"),
		category=SCRIPT_CATEGORY,
		gesture="kb:NVDA+alt+g",
	)
	def script_askAI(self, gesture):
		if globalVars.appArgs.secure:
			return
		wx.CallAfter(dialogs.openAskDialog)

	@script(
		# Translators: Description of the command that opens the list of saved conversations.
		description=_("Show Ask AI conversations"),
		category=SCRIPT_CATEGORY,
	)
	def script_showConversations(self, gesture):
		if globalVars.appArgs.secure:
			return
		wx.CallAfter(dialogs.openConversationManager)

	@script(
		# Translators: Description of the command that repeats the last answer.
		description=_("Speaks the last answer from the AI. Press twice to show it in a window you can review"),
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
			log.debugWarning("Ask AI: could not load the current conversation", exc_info=True)
		if not dialogs.state.lastAnswer:
			# Translators: Reported when no answer has been received yet.
			ui.message(_("No answer yet."))
			return
		if scriptHandler.getLastScriptRepeatCount() >= 1:
			# The window shows the answer exactly as the AI wrote it, including code.
			# Translators: Title of the window showing the last answer.
			ui.browseableMessage(dialogs.state.lastAnswer, _("Last answer"))
		else:
			ui.message(dialogs.state.lastAnswerSpoken)

	@script(
		# Translators: Description of the command that reports what Ask AI is doing.
		description=_("Reports what Ask AI is doing: answering, dictating, waiting for sign-in or idle"),
		category=SCRIPT_CATEGORY,
		gesture="kb:NVDA+alt+shift+s",
		speakOnDemand=True,
	)
	def script_reportStatus(self, gesture):
		if globalVars.appArgs.secure:
			return
		try:
			message = dialogs.currentStatus()
		except Exception:
			log.error("Ask AI: could not build the status", exc_info=True)
			return
		ui.message(message)

	@script(
		# Translators: Description of the command that attaches the selected or current text to a question.
		description=_(
			"Ask about this: attaches the selected text, or the text of the navigator object, to the next question"
		),
		category=SCRIPT_CATEGORY,
		gesture="kb:NVDA+alt+a",
	)
	def script_askAboutThis(self, gesture):
		if globalVars.appArgs.secure:
			return
		try:
			screenContext = captureScreenContext()
		except context.ContextError as e:
			if e.reason == context.REASON_PROTECTED:
				# Translators: Reported when "Ask about this" is used on a password or other protected field.
				ui.message(_("Can't use protected or password fields"))
			else:
				# Translators: Reported when "Ask about this" finds no text to attach.
				ui.message(_("No text here to ask about"))
			return
		except Exception:
			log.error("Ask AI: could not capture the screen text", exc_info=True)
			# Translators: Reported when the screen text could not be read.
			ui.message(_("Could not read the text here"))
			return
		wx.CallAfter(dialogs.openAskDialogWithContext, screenContext)

	def terminate(self):
		try:
			updateCheck.terminate()
		except Exception:
			log.debugWarning("Ask AI: error stopping the update check", exc_info=True)
		timer, self._oldAddonTimer = self._oldAddonTimer, None
		if timer is not None:
			try:
				timer.Stop()
			except Exception:
				pass
		try:
			# Kills a running question and sign-in synchronously, then closes the dialog.
			dialogs.shutdown()
		except Exception:
			log.debugWarning("Ask AI: error during shutdown", exc_info=True)
		try:
			settingsDialogs.NVDASettingsDialog.categoryClasses.remove(dialogs.AskAISettingsPanel)
		except ValueError:
			pass
		for item in (self.toolsMenuItem, self.conversationsMenuItem, self.updatesMenuItem):
			if item is not None:
				try:
					gui.mainFrame.sysTrayIcon.toolsMenu.Remove(item)
				except Exception:
					log.debugWarning("Ask AI: error removing menu item", exc_info=True)
		self.toolsMenuItem = None
		self.conversationsMenuItem = None
		self.updatesMenuItem = None
		super().terminate()
