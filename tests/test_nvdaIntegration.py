"""Smoke test of the NVDA-facing modules against minimal stand-ins for NVDA's modules.

This cannot prove the wx UI works (that needs NVDA on Windows), but it catches import errors,
missing names, wrong registration/cleanup of the settings panel, menu item and gestures.
"""

import importlib
import inspect
import os
import re
import sys
import time
import types
import unittest
from unittest import mock

import helpers


class _Anything:
	"""Accepts any call or attribute access."""

	def __init__(self, *args, **kwargs):
		pass

	def __call__(self, *args, **kwargs):
		return _Anything()

	def __getattr__(self, name):
		return _Anything()


def _module(name, **attrs):
	module = types.ModuleType(name)
	module.__dict__.update(attrs)
	return module


def _initTranslation():
	caller = inspect.currentframe().f_back
	try:
		caller.f_globals["_"] = lambda text: text
	finally:
		del caller


def _script(**kwargs):
	def decorator(func):
		func.__doc__ = kwargs.get("description")
		func.category = kwargs.get("category")
		func.gestures = [kwargs["gesture"]] if "gesture" in kwargs else []
		func.speakOnDemand = kwargs.get("speakOnDemand", False)
		return func

	return decorator


class _Menu:
	def __init__(self):
		self.items = []

	def Append(self, id, label, helpText=""):
		item = ("item", label)
		self.items.append(item)
		return item

	def Remove(self, item):
		self.items.remove(item)


class _Conf(dict):
	def __init__(self):
		super().__init__()
		self.spec = {}


class _BaseGlobalPlugin:
	def __init__(self):
		pass

	def terminate(self):
		pass


def buildFakeNvda(configPath):
	wx = _module(
		"wx",
		Dialog=_Anything,
		Panel=_Anything,
		Timer=_Anything,
		CallAfter=lambda func, *a, **k: func(*a, **k),
		CallLater=_Anything,
		ID_ANY=-1,
	)
	wx.__getattr__ = lambda name: 0  # Constants such as wx.TE_MULTILINE, wx.EVT_BUTTON.
	toolsMenu = _Menu()
	sysTrayIcon = types.SimpleNamespace(toolsMenu=toolsMenu, Bind=lambda *a, **k: None)
	settingsDialogs = _module(
		"gui.settingsDialogs",
		NVDASettingsDialog=type("NVDASettingsDialog", (), {"categoryClasses": []}),
		SettingsPanel=type("SettingsPanel", (), {"isValid": lambda self: True}),
	)
	gui = _module(
		"gui",
		mainFrame=types.SimpleNamespace(sysTrayIcon=sysTrayIcon),
		guiHelper=_module("gui.guiHelper"),
		nvdaControls=_module("gui.nvdaControls"),
		settingsDialogs=settingsDialogs,
		messageBox=lambda *a, **k: None,
	)
	spoken = []
	return {
		"wx": wx,
		"addonHandler": _module("addonHandler", initTranslation=_initTranslation),
		"api": _module("api", copyToClip=lambda text: True),
		"config": _module("config", conf=_Conf()),
		"globalPluginHandler": _module("globalPluginHandler", GlobalPlugin=_BaseGlobalPlugin),
		"globalVars": _module(
			"globalVars",
			appArgs=types.SimpleNamespace(secure=False, configPath=configPath),
		),
		"gui": gui,
		"gui.guiHelper": gui.guiHelper,
		"gui.nvdaControls": gui.nvdaControls,
		"gui.settingsDialogs": settingsDialogs,
		"scriptHandler": _module("scriptHandler", script=_script, getLastScriptRepeatCount=lambda: 0),
		"tones": _module("tones", beep=lambda *a, **k: None),
		"ui": _module(
			"ui",
			message=spoken.append,
			browseableMessage=lambda text, title=None, **k: spoken.append(("browseable", text)),
		),
		"logHandler": _module("logHandler", log=_Anything()),
	}, spoken


class TestPluginWithFakeNvda(unittest.TestCase):
	def setUp(self):
		self.fakeModules, self.spoken = buildFakeNvda(os.path.join(helpers.ROOT, "tmp-config"))
		self.savedModules = {name: sys.modules.get(name) for name in self.fakeModules}
		sys.modules.update(self.fakeModules)
		sys.path.insert(0, os.path.join(helpers.ROOT, "addon", "globalPlugins"))
		for name in list(sys.modules):
			if name == "askGPT" or name.startswith("askGPT."):
				del sys.modules[name]
		self.package = importlib.import_module("askGPT")
		self.dialogs = importlib.import_module("askGPT.dialogs")
		self.codexClient = sys.modules["askGPT.codexClient"]
		self.conf = {
			"codexPath": "",
			"model": "",
			"reasoningEffort": "low",
			"instructions": "",
			"speakAnswers": True,
			"keepContext": True,
			"beepWhileWaiting": True,
			"timeout": 180,
		}
		self.fakeModules["config"].conf["askGPT"] = self.conf

	def tearDown(self):
		for name in list(sys.modules):
			if name == "askGPT" or name.startswith("askGPT."):
				del sys.modules[name]
		sys.path.remove(os.path.join(helpers.ROOT, "addon", "globalPlugins"))
		for name, module in self.savedModules.items():
			if module is None:
				sys.modules.pop(name, None)
			else:
				sys.modules[name] = module

	def test_configSpec(self):
		spec = self.fakeModules["config"].conf.spec["askGPT"]
		self.assertEqual(
			set(spec),
			{
				"codexPath",
				"model",
				"reasoningEffort",
				"instructions",
				"speakAnswers",
				"keepContext",
				"beepWhileWaiting",
				"timeout",
			},
		)
		# A13: beeping is on by default and can be switched off.
		self.assertEqual(spec["beepWhileWaiting"], "boolean(default=True)")
		self.assertEqual(spec["timeout"], "integer(default=180, min=30, max=600)")
		self.assertIn('default="low"', spec["reasoningEffort"])
		# A double quote inside the default would break the configobj spec.
		self.assertNotIn('"', spec["instructions"][len('string(default="'):-2])

	def test_lifecycle(self):
		gui = self.fakeModules["gui"]
		categories = gui.settingsDialogs.NVDASettingsDialog.categoryClasses
		plugin = self.package.GlobalPlugin()
		self.assertIn(self.dialogs.AskGPTSettingsPanel, categories)
		self.assertEqual(len(gui.mainFrame.sysTrayIcon.toolsMenu.items), 1)
		# C6: the working folder is not inside NVDA's configuration folder.
		self.assertEqual(self.dialogs.workDir, self.codexClient.defaultWorkDir())
		self.assertNotIn("tmp-config", self.dialogs.workDir)
		plugin.terminate()
		self.assertNotIn(self.dialogs.AskGPTSettingsPanel, categories)
		self.assertEqual(gui.mainFrame.sysTrayIcon.toolsMenu.items, [])

	def test_secureModeRegistersNothing(self):
		self.fakeModules["globalVars"].appArgs.secure = True
		gui = self.fakeModules["gui"]
		plugin = self.package.GlobalPlugin()
		self.assertEqual(gui.settingsDialogs.NVDASettingsDialog.categoryClasses, [])
		self.assertEqual(gui.mainFrame.sysTrayIcon.toolsMenu.items, [])
		plugin.terminate()

	def test_scripts(self):
		cls = self.package.GlobalPlugin
		self.assertEqual(cls.script_askGPT.gestures, ["kb:NVDA+alt+g"])
		self.assertEqual(cls.script_askGPT.category, "Ask GPT")
		self.assertEqual(cls.script_speakLastAnswer.gestures, ["kb:NVDA+alt+shift+g"])
		self.assertTrue(cls.script_speakLastAnswer.speakOnDemand)

	def test_speakLastAnswer(self):
		plugin = self.package.GlobalPlugin()
		plugin.script_speakLastAnswer(None)
		self.assertEqual(self.spoken[-1], "No answer from GPT yet.")
		# C3: speech gets the stripped text, the browseable window the raw answer.
		self.dialogs.state.lastAnswer = "**Paris** is the capital. Use `a_b_c`."
		plugin.script_speakLastAnswer(None)
		self.assertEqual(self.spoken[-1], "Paris is the capital. Use a_b_c.")
		self.fakeModules["scriptHandler"].getLastScriptRepeatCount = lambda: 1
		plugin.script_speakLastAnswer(None)
		self.assertEqual(self.spoken[-1], ("browseable", "**Paris** is the capital. Use `a_b_c`."))
		plugin.terminate()

	def test_friendlyErrorsNeverSpeakTechnicalDetail(self):
		codexClient = self.codexClient
		CodexError = codexClient.CodexError
		for kind in (
			codexClient.KIND_NOT_INSTALLED,
			codexClient.KIND_NOT_LOGGED_IN,
			codexClient.KIND_USAGE_LIMIT,
			codexClient.KIND_NETWORK,
			codexClient.KIND_TIMEOUT,
			codexClient.KIND_CANCELLED,
			codexClient.KIND_CONFIG,
			codexClient.KIND_OTHER,
		):
			error = CodexError(kind, "Plain sentence.", "ERROR: secret technical detail")
			message = self.dialogs.friendlyError(error)
			self.assertTrue(message, kind)
			self.assertNotIn("secret technical detail", message, kind)
			# A4: the technical detail only appears in the transcript, after a label.
			self.assertIn("Technical details: ERROR: secret technical detail", self.dialogs.transcriptErrorText(error))
		self.assertEqual(
			self.dialogs.friendlyError(CodexError(codexClient.KIND_OTHER, "Codex could not answer (exit code 1).", "x")),
			"Codex could not answer (exit code 1).",
		)
		self.assertEqual(
			self.dialogs.friendlyError(CodexError(codexClient.KIND_NETWORK, "", "")),
			"Couldn't reach ChatGPT. Check your internet connection and try again.",
		)
		custom = CodexError(codexClient.KIND_NOT_INSTALLED, "x", "D:\\tools\\codex.exe", codexClient.REASON_CUSTOM_PATH)
		self.assertIn("D:\\tools\\codex.exe", self.dialogs.friendlyError(custom))
		self.assertIn("Ask GPT settings", self.dialogs.friendlyError(custom))
		node = CodexError(codexClient.KIND_NOT_INSTALLED, "x", "", codexClient.REASON_NODE_MISSING)
		self.assertIn("winget install OpenJS.NodeJS.LTS", self.dialogs.friendlyError(node))
		self.assertIn("npm install -g @openai/codex", self.dialogs.friendlyError(CodexError("notInstalled", "")))
		empty = CodexError(codexClient.KIND_OTHER, "x", "", codexClient.REASON_EMPTY_ANSWER)
		self.assertIn("empty answer", self.dialogs.friendlyError(empty))

	def test_shortStatusIsOneSentence(self):
		codexClient = self.codexClient
		for kind, reason in (
			(codexClient.KIND_NOT_INSTALLED, ""),
			(codexClient.KIND_NOT_INSTALLED, codexClient.REASON_NODE_MISSING),
			(codexClient.KIND_NOT_INSTALLED, codexClient.REASON_CUSTOM_PATH),
			(codexClient.KIND_NETWORK, ""),
			(codexClient.KIND_TIMEOUT, ""),
			(codexClient.KIND_OTHER, ""),
		):
			status = self.dialogs.shortStatusForError(codexClient.CodexError(kind, "a. b. c.", "detail. more."))
			self.assertNotIn(".", status, status)

	def test_conversationStateCaps(self):
		state = self.dialogs.ConversationState()
		for i in range(80):
			state.addExchange("q%d" % i, "a%d" % i)
		self.assertEqual(len(state.history), self.dialogs.MAX_STORED_MESSAGES)
		self.assertEqual(state.history[-1], ("assistant", "a79"))
		# C9: the Conversation transcript is capped too, and the last-answer index follows the trim.
		limit = self.dialogs.MAX_TRANSCRIPT_ENTRIES
		for i in range(limit - 1):
			state.appendTranscript("You: %d" % i)
		self.assertEqual(state.appendTranscript("GPT: answer", isAnswer=True), 0)
		self.assertEqual(state.lastAnswerIndex, limit - 1)
		self.assertEqual(state.appendTranscript("You: more"), 1)
		self.assertEqual(len(state.transcript), limit)
		self.assertEqual(state.lastAnswerIndex, limit - 2)
		self.assertEqual(state.transcript[state.lastAnswerIndex], "GPT: answer")
		for i in range(limit):
			state.appendTranscript("filler")
		self.assertIsNone(state.lastAnswerIndex)
		state.lastAnswer = "**raw**"
		self.assertEqual(state.lastAnswerSpoken, "raw")
		state.clear()
		self.assertEqual((state.history, state.transcript, state.lastAnswer, state.lastAnswerIndex), ([], [], "", None))

	def _pending(self, question="What is up?"):
		client = mock.Mock()
		pending = self.dialogs.PendingRequest(client, question)
		self.dialogs.state.pending = pending
		return pending

	def test_answerArrivingAfterDialogClosedIsSpoken(self):
		# A7: the request outlives the dialog and its answer is spoken.
		dialogs = self.dialogs
		pending = self._pending()
		dialogs._onRequestDone(pending, "**Sunny** today.", None)
		self.assertEqual(self.spoken[-1], "Sunny today.")
		self.assertIsNone(dialogs.state.pending)
		self.assertEqual(dialogs.state.lastAnswer, "**Sunny** today.")
		self.assertEqual(dialogs.state.history[-2:], [("user", "What is up?"), ("assistant", "**Sunny** today.")])
		self.assertEqual(dialogs.state.transcript[-1], "GPT: Sunny today.")
		self.conf["speakAnswers"] = False
		pending = self._pending()
		dialogs._onRequestDone(pending, "Hidden.", None)
		self.assertIn("NVDA+Alt+Shift+G", self.spoken[-1])

	def test_errorAfterDialogClosedSpeaksFriendlyMessageOnly(self):
		dialogs = self.dialogs
		pending = self._pending()
		error = self.codexClient.CodexError(self.codexClient.KIND_NETWORK, "x", "ERROR: stream disconnected")
		dialogs._onRequestDone(pending, None, error)
		self.assertEqual(self.spoken[-1], "Couldn't reach ChatGPT. Check your internet connection and try again.")
		self.assertIn("Technical details: ERROR: stream disconnected", dialogs.state.transcript[-1])

	def test_cancelledRequestIsIgnored(self):
		dialogs = self.dialogs
		pending = self._pending()
		self.assertIs(dialogs.cancelRequest(), pending)
		self.assertTrue(pending.cancelEvent.is_set())
		count = len(self.spoken)
		dialogs._onRequestDone(pending, "late answer", None)
		self.assertEqual(len(self.spoken), count)
		self.assertEqual(dialogs.state.lastAnswer, "")

	def test_shutdownKillsRequestAndLoginSynchronously(self):
		# C5: terminate() must kill the running codex and any sign-in.
		dialogs = self.dialogs
		pending = self._pending()
		loginClient = mock.Mock()
		dialogs._loginClient = loginClient
		plugin = self.package.GlobalPlugin()
		plugin.terminate()
		pending.client.terminate.assert_called_once_with()
		self.assertTrue(pending.cancelEvent.is_set())
		loginClient.cancelLogin.assert_called_once_with()
		self.assertIsNone(dialogs._loginClient)
		self.assertIsNone(dialogs.state.pending)

	def test_finishLoginCompareAndClear(self):
		# C8: a finished login only acts if it is still the current one.
		dialogs = self.dialogs
		stale = mock.Mock()
		current = mock.Mock()
		dialogs._loginClient = current
		dialogs._finishLogin(stale, timedOut=True)
		self.assertIs(dialogs._loginClient, current)
		stale.cancelLogin.assert_not_called()
		self.assertEqual(self.spoken, [])
		# A2: an expired sign-in is announced.
		dialogs._finishLogin(current, timedOut=True)
		self.assertIsNone(dialogs._loginClient)
		current.cancelLogin.assert_called_once_with()
		self.assertEqual(self.spoken[-1], "Sign-in was not completed. Press Sign in to try again.")

	def test_finishLoginAnnouncesSuccess(self):
		dialogs = self.dialogs
		client = mock.Mock()
		client.loginStatus.return_value = (True, "Logged in using ChatGPT")
		dialogs._loginClient = client
		dialogs._finishLogin(client, timedOut=False)
		for _ in range(200):
			if self.spoken:
				break
			time.sleep(0.01)
		self.assertEqual(self.spoken, ["Signed in with ChatGPT."])

	def test_onceRunsOnce(self):
		calls = []
		wrapped = self.dialogs._once(lambda: calls.append(1))
		wrapped()
		wrapped()
		self.assertEqual(calls, [1])


def _mnemonics(source, className):
	"""Letters after "&" in the translatable labels of one class in dialogs.py."""
	start = source.index("class %s(" % className)
	nextClass = re.search(r"^(class |def )", source[start + 1:], re.MULTILINE)
	body = source[start:start + 1 + nextClass.start()] if nextClass else source[start:]
	letters = []
	for label in re.findall(r'_\(\s*"((?:[^"\\]|\\.)*)"', body):
		match = re.search(r"&([^&\s])", label.replace("&&", ""))
		if match:
			letters.append((match.group(1).lower(), label))
	return letters


class TestMnemonics(unittest.TestCase):
	def setUp(self):
		with open(os.path.join(helpers.PACKAGE_DIR, "dialogs.py"), encoding="utf-8") as f:
			self.source = f.read()

	def test_settingsPanelMnemonicsAreUniqueAndAvoidDialogButtons(self):
		# A1: NVDA's Settings dialog owns O (OK), C (Cancel, Categories) and A (Apply).
		letters = _mnemonics(self.source, "AskGPTSettingsPanel")
		self.assertGreaterEqual(len(letters), 12)
		used = [letter for letter, label in letters]
		self.assertEqual(len(used), len(set(used)), letters)
		for letter, label in letters:
			self.assertNotIn(letter, "oca", label)
		labels = [label for letter, label in letters]
		self.assertIn("Sign o&ut", labels)
		self.assertIn("S&peak answers automatically", labels)
		self.assertIn("Cop&y sign-in link", labels)

	def test_askDialogMnemonicsAreUnique(self):
		letters = _mnemonics(self.source, "AskGPTDialog")
		used = [letter for letter, label in letters if label != "&Ask"]  # &Ask and &Cancel share one button.
		self.assertEqual(len(used), len(set(used)), letters)
		self.assertIn("&Question:", [label for letter, label in letters])


if __name__ == "__main__":
	unittest.main()
