"""Smoke test of the NVDA-facing modules against minimal stand-ins for NVDA's modules.

This cannot prove the wx UI works (that needs NVDA on Windows), but it catches import errors,
missing names, wrong registration/cleanup of the settings panel, menu item and gestures.
"""

import importlib
import inspect
import os
import sys
import types
import unittest

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
		"ui": _module("ui", message=spoken.append, browseableMessage=lambda *a, **k: None),
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
			{"codexPath", "model", "reasoningEffort", "instructions", "speakAnswers", "keepContext", "timeout"},
		)
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
		self.assertEqual(
			self.dialogs.workDir,
			os.path.join(helpers.ROOT, "tmp-config", "askGPT-workdir"),
		)
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
		self.dialogs.state.lastAnswer = "Paris."
		plugin.script_speakLastAnswer(None)
		self.assertEqual(self.spoken[-1], "Paris.")
		plugin.terminate()

	def test_friendlyErrors(self):
		codexClient = sys.modules["askGPT.codexClient"]
		for kind in (
			codexClient.KIND_NOT_INSTALLED,
			codexClient.KIND_NOT_LOGGED_IN,
			codexClient.KIND_USAGE_LIMIT,
			codexClient.KIND_TIMEOUT,
			codexClient.KIND_CANCELLED,
			codexClient.KIND_CONFIG,
			codexClient.KIND_OTHER,
		):
			message = self.dialogs.friendlyError(codexClient.CodexError(kind, "english", "tail of stderr"))
			self.assertTrue(message, kind)
		self.assertIn("npm install -g @openai/codex", self.dialogs.friendlyError(codexClient.CodexError("notInstalled", "")))
		self.assertIn("tail of stderr", self.dialogs.friendlyError(codexClient.CodexError("other", "x", "tail of stderr")))

	def test_conversationStateCap(self):
		state = self.dialogs.ConversationState()
		for i in range(80):
			state.addExchange("q%d" % i, "a%d" % i)
		self.assertEqual(len(state.history), self.dialogs.MAX_STORED_MESSAGES)
		self.assertEqual(state.history[-1], ("assistant", "a79"))
		state.clear()
		self.assertEqual((state.history, state.transcript, state.lastAnswer), ([], [], ""))


if __name__ == "__main__":
	unittest.main()
