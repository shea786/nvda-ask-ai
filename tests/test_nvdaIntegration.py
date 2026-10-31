"""Smoke test of the NVDA-facing modules against minimal stand-ins for NVDA's modules.

This cannot prove the wx UI works (that needs NVDA on Windows), but it catches import errors,
missing names, wrong registration/cleanup of the settings panel, menu item and gestures.
"""

import importlib
import json
import inspect
import os
import re
import shutil
import sys
import tempfile
import threading
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
		caller.f_globals["ngettext"] = lambda singular, plural, count: singular if count == 1 else plural
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
		dpiScalingHelper=_module(
			"gui.dpiScalingHelper",
			DpiScalingHelperMixinWithoutInit=type("DpiScalingHelperMixinWithoutInit", (), {}),
		),
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
		"gui.dpiScalingHelper": gui.dpiScalingHelper,
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


class FakeClient:
	"""Stands in for a provider inside the request lifecycle (no Codex or Claude Code, no subscription usage)."""

	providerId = "codex"

	def __init__(self, answer="Fine.", error=None, release=None, onAsk=None):
		self.answer = answer
		self.error = error
		self.release = release
		self.onAsk = onAsk
		self.prompts = []
		self.terminated = False

	def ask(self, prompt, cancelEvent=None):
		self.prompts.append(prompt)
		if self.release is not None:
			self.release.wait(5)
		if cancelEvent is not None and cancelEvent.is_set():
			raise sys.modules["askAI.providers.base"].ProviderError("cancelled", "The request was cancelled.")
		if self.onAsk is not None:
			self.onAsk()
		if self.error is not None:
			raise self.error
		return self.answer

	def terminate(self):
		self.terminated = True


class FakeNvdaTestCase(unittest.TestCase):
	"""Imports the add-on against stand-ins for NVDA's modules, with a temporary conversations folder."""

	def setUp(self):
		self.fakeModules, self.spoken = buildFakeNvda(os.path.join(helpers.ROOT, "tmp-config"))
		self.savedModules = {name: sys.modules.get(name) for name in self.fakeModules}
		sys.modules.update(self.fakeModules)
		sys.path.insert(0, os.path.join(helpers.ROOT, "addon", "globalPlugins"))
		for name in list(sys.modules):
			if name == "askAI" or name.startswith("askAI."):
				del sys.modules[name]
		self.package = importlib.import_module("askAI")
		self.dialogs = importlib.import_module("askAI.dialogs")
		self.codexClient = sys.modules["askAI.providers.base"]
		self.providers = sys.modules["askAI.providers"]
		self.conversations = sys.modules["askAI.conversations"]
		self.tempDir = tempfile.mkdtemp(prefix="askai-nvda-")
		self.conversationsDir = os.path.join(self.tempDir, "conversations")
		self.conf = {
			"provider": "codex",
			"codexPath": "",
			"model": "",
			"reasoningEffort": "low",
			"claudePath": "",
			"claudeModel": "",
			"claudeReasoningEffort": "low",
			"instructions": "",
			"speakAnswers": True,
			"keepContext": True,
			"beepWhileWaiting": True,
			"allowProjectWrites": True,
			"conversationsDir": self.conversationsDir,
			"dictationSilenceTimeout": 4,
			"timeout": 180,
			"autoUpdateCheck": True,
			"lastUpdateCheck": 0.0,
			# Migration ran already, unless a test says otherwise.
			"migratedFromAskGPT": True,
			"askedToRemoveAskGPT": True,
		}
		self.fakeModules["config"].conf["askAI"] = self.conf
		# Never read the developer's real Codex model cache in these tests.
		self.dialogs.state._models = {"codex": [], "claude": self.providers.claude.presetModels()}

	def tearDown(self):
		for name in list(sys.modules):
			if name == "askAI" or name.startswith("askAI."):
				del sys.modules[name]
		sys.path.remove(os.path.join(helpers.ROOT, "addon", "globalPlugins"))
		for name, module in self.savedModules.items():
			if module is None:
				sys.modules.pop(name, None)
			else:
				sys.modules[name] = module
		shutil.rmtree(self.tempDir, ignore_errors=True)

	def waitUntil(self, condition, timeout=5):
		deadline = time.monotonic() + timeout
		while not condition():
			if time.monotonic() > deadline:
				self.fail("condition not reached")
			time.sleep(0.01)

	def waitForRequests(self):
		"""Join the request worker threads (the fake wx.CallAfter runs their callbacks in them)."""
		for thread in threading.enumerate():
			if thread.name == "askAI.ask":
				thread.join(5)
				self.assertFalse(thread.is_alive())

	def useClient(self, client):
		self.dialogs.makeClient = lambda executablePath=None, conversation=None, providerId=None: client
		return client

	def _conversation(self, *messages, **kwargs):
		conversation = self.conversations.Conversation(**kwargs)
		for role, text in messages:
			conversation.addMessage(role, text)
		return conversation


class TestPluginWithFakeNvda(FakeNvdaTestCase):
	def test_configSpec(self):
		spec = self.fakeModules["config"].conf.spec["askAI"]
		self.assertEqual(
			set(spec),
			{
				"provider",
				"codexPath",
				"model",
				"reasoningEffort",
				"claudePath",
				"claudeModel",
				"claudeReasoningEffort",
				"instructions",
				"speakAnswers",
				"keepContext",
				"beepWhileWaiting",
				"allowProjectWrites",
				"conversationsDir",
				"dictationSilenceTimeout",
				"timeout",
				"autoUpdateCheck",
				"lastUpdateCheck",
				"migratedFromAskGPT",
				"askedToRemoveAskGPT",
			},
		)
		self.assertEqual(spec["provider"], 'string(default="codex")')
		self.assertEqual(spec["autoUpdateCheck"], "boolean(default=True)")
		self.assertEqual(spec["claudeReasoningEffort"], 'string(default="low")')
		# A13: beeping is on by default and can be switched off.
		self.assertEqual(spec["beepWhileWaiting"], "boolean(default=True)")
		self.assertEqual(spec["allowProjectWrites"], "boolean(default=True)")
		self.assertEqual(spec["conversationsDir"], 'string(default="")')
		self.assertEqual(spec["dictationSilenceTimeout"], "integer(default=4, min=2, max=15)")
		self.assertEqual(spec["timeout"], "integer(default=180, min=30, max=600)")
		# Free text, because models offer levels such as xhigh.
		self.assertEqual(spec["reasoningEffort"], 'string(default="low")')
		# A double quote inside the default would break the configobj spec.
		self.assertNotIn('"', spec["instructions"][len('string(default="'):-2])

	def test_lifecycle(self):
		gui = self.fakeModules["gui"]
		categories = gui.settingsDialogs.NVDASettingsDialog.categoryClasses
		plugin = self.package.GlobalPlugin()
		self.assertIn(self.dialogs.AskAISettingsPanel, categories)
		self.assertEqual(
			[label for _item, label in gui.mainFrame.sysTrayIcon.toolsMenu.items],
			["As&k AI...", "Ask AI con&versations...", "Check for Ask AI &updates..."],
		)
		# C6: the working folder is not inside NVDA's configuration folder.
		self.assertEqual(self.dialogs.workDir, self.codexClient.defaultWorkDir())
		self.assertNotIn("tmp-config", self.dialogs.workDir)
		plugin.terminate()
		self.assertNotIn(self.dialogs.AskAISettingsPanel, categories)
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
		self.assertEqual(cls.script_askAI.gestures, ["kb:NVDA+alt+g"])
		self.assertEqual(cls.script_askAI.category, "Ask AI")
		self.assertEqual(cls.script_speakLastAnswer.gestures, ["kb:NVDA+alt+shift+g"])
		self.assertTrue(cls.script_speakLastAnswer.speakOnDemand)
		# Feature 4: no default gesture, remappable in Input Gestures.
		self.assertEqual(cls.script_showConversations.gestures, [])
		self.assertEqual(cls.script_showConversations.__doc__, "Show Ask AI conversations")
		self.assertEqual(cls.script_showConversations.category, "Ask AI")

	def test_speakLastAnswer(self):
		plugin = self.package.GlobalPlugin()
		plugin.script_speakLastAnswer(None)
		self.assertEqual(self.spoken[-1], "No answer yet.")
		# C3: speech gets the stripped text, the browseable window the raw answer.
		self.dialogs.state.current = self._conversation(("user", "q"), ("assistant", "**Paris** is the capital. Use `a_b_c`."))
		plugin.script_speakLastAnswer(None)
		self.assertEqual(self.spoken[-1], "Paris is the capital. Use a_b_c.")
		self.fakeModules["scriptHandler"].getLastScriptRepeatCount = lambda: 1
		plugin.script_speakLastAnswer(None)
		self.assertEqual(self.spoken[-1], ("browseable", "**Paris** is the capital. Use `a_b_c`."))
		plugin.terminate()

	def test_speakLastAnswerAfterRestartUsesNewestSavedConversation(self):
		store = self.conversations.ConversationStore(self.conversationsDir)
		old = self._conversation(("user", "old"), ("assistant", "Old answer."))
		old.updated -= 100
		store.save(old)
		store.save(self._conversation(("user", "new"), ("assistant", "New answer.")))
		plugin = self.package.GlobalPlugin()
		plugin.script_speakLastAnswer(None)
		self.assertEqual(self.spoken[-1], "New answer.")
		plugin.terminate()

	def test_ensureCurrentAndNewConversationDefaults(self):
		state = self.dialogs.state
		self.conf["model"] = "gpt-6.1-sol"
		self.conf["reasoningEffort"] = "xhigh"
		conversation = state.ensureCurrent()
		self.assertTrue(conversation.isEmpty)
		self.assertEqual((conversation.model, conversation.reasoningEffort), ("gpt-6.1-sol", "xhigh"))
		self.assertFalse(state.save(), "an empty conversation is not written")
		self.assertEqual(os.listdir(self.tempDir), [])
		conversation.projectFolder = self.tempDir
		self.assertTrue(state.save(), "a conversation with a project folder is kept")

	def test_saveFailureIsAnnounced(self):
		blocker = os.path.join(self.tempDir, "file")
		with open(blocker, "w") as f:
			f.write("x")
		self.conf["conversationsDir"] = blocker  # A file, not a folder.
		state = self.dialogs.state
		state.current = self._conversation(("user", "q"))
		self.assertFalse(state.save())
		self.assertIn("Could not save the conversation", self.spoken[-1])

	def test_friendlyErrorsNeverSpeakTechnicalDetail(self):
		codexClient = self.codexClient
		CodexError = codexClient.ProviderError
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
		custom = CodexError(codexClient.KIND_NOT_INSTALLED, "x", "D:\\\\tools\\\\codex.exe", codexClient.REASON_CUSTOM_PATH)
		self.assertIn("D:\\\\tools\\\\codex.exe", self.dialogs.friendlyError(custom))
		node = CodexError(codexClient.KIND_NOT_INSTALLED, "x", "", codexClient.REASON_NODE_MISSING)
		self.assertIn("winget install OpenJS.NodeJS.LTS", self.dialogs.friendlyError(node))
		self.assertIn("npm install -g @openai/codex", self.dialogs.friendlyError(CodexError("notInstalled", "")))
		empty = CodexError(codexClient.KIND_OTHER, "x", "", codexClient.REASON_EMPTY_ANSWER)
		self.assertIn("empty answer", self.dialogs.friendlyError(empty))
		missing = CodexError(codexClient.KIND_CONFIG, "x", "D:\\\\gone", codexClient.REASON_PROJECT_MISSING)
		self.assertEqual(
			self.dialogs.friendlyError(missing),
			"The project folder no longer exists: D:\\\\gone. Choose another folder or clear it.",
		)
		self.assertNotIn("Technical details", self.dialogs.transcriptErrorText(missing))

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
			status = self.dialogs.shortStatusForError(codexClient.ProviderError(kind, "a. b. c.", "detail. more."))
			self.assertNotIn(".", status, status)

	def test_transcriptEntries(self):
		conversation = self._conversation(
			("user", "Hi"),
			("assistant", "**Hello**"),
			("error", "Oops"),
			("note", "Created: a.docx"),
		)
		self.assertEqual(
			self.dialogs.transcriptEntries(conversation),
			[("You: Hi", False), ("ChatGPT: Hello", True), ("Error: Oops", False), ("Created: a.docx", False)],
		)
		# C9: the Conversation box shows at most MAX_TRANSCRIPT_ENTRIES of the newest messages.
		for i in range(150):
			conversation.addMessage("user", "q%d" % i)
		entries = self.dialogs.transcriptEntries(conversation)
		self.assertEqual(len(entries), self.dialogs.MAX_TRANSCRIPT_ENTRIES)
		self.assertEqual(entries[-1], ("You: q149", False))

	def test_fileChangesText(self):
		self.assertEqual(self.dialogs.fileChangesText([], []), "")
		self.assertEqual(self.dialogs.fileChangesText(["a.docx", "b.pdf"], []), "Created: a.docx, b.pdf")
		self.assertEqual(
			self.dialogs.fileChangesText(["%d.txt" % i for i in range(8)], ["x.txt"]),
			"Created: 0.txt, 1.txt, 2.txt, 3.txt, 4.txt and 3 more. Changed: x.txt",
		)

	def test_effortLabels(self):
		self.assertEqual(self.dialogs.effortLabel("low"), "Low (fastest)")
		self.assertEqual(self.dialogs.effortLabel("xhigh"), "Extra high")
		self.assertEqual(self.dialogs.effortLabel("turbo"), "Turbo")

	def test_makeClientUsesTheConversation(self):
		catalog = sys.modules["askAI.providers.modelCatalog"]
		self.dialogs.state._models["codex"] = [catalog.ModelInfo("gpt-6.1-sol", "GPT 6.1 Sol", ("low", "medium", "xhigh"), "low")]
		self.conf["allowProjectWrites"] = False
		conversation = self._conversation(model="gpt-6.1-sol", reasoningEffort="xhigh", projectFolder=self.tempDir)
		client = self.dialogs.makeClient(conversation=conversation)
		self.assertEqual((client.model, client.reasoningEffort), ("gpt-6.1-sol", "xhigh"))
		self.assertEqual(client.projectFolder, self.tempDir)
		self.assertFalse(client.allowProjectWrites)
		# An effort the model does not offer falls back to the model's default.
		conversation.reasoningEffort = "ultra"
		self.assertEqual(self.dialogs.makeClient(conversation=conversation).reasoningEffort, "low")

	def test_answerIsSavedAndSpokenAfterDialogClosed(self):
		# A7: the request outlives the dialog and its answer is spoken.
		dialogs = self.dialogs
		client = self.useClient(FakeClient(answer="**Sunny** today."))
		conversation = self._conversation()
		dialogs.state.current = conversation
		dialogs.startRequest("What is up?", conversation)
		self.waitForRequests()
		self.assertEqual(self.spoken[-1], "Sunny today.")
		self.assertEqual(conversation.lastAnswer, "**Sunny** today.")
		self.assertEqual(conversation.title, "What is up?")
		self.assertEqual(conversation.history, [("user", "What is up?"), ("assistant", "**Sunny** today.")])
		saved = self.conversations.ConversationStore(self.conversationsDir).load(conversation.id)
		self.assertEqual(saved.lastAnswer, "**Sunny** today.", "saved after each completed turn")
		self.assertIn("Question: What is up?", client.prompts[0])
		self.conf["speakAnswers"] = False
		dialogs.startRequest("Again?", conversation)
		self.waitForRequests()
		self.assertIn("NVDA+Alt+Shift+G", self.spoken[-1])

	def test_answerGoesToTheConversationItWasAskedIn(self):
		dialogs = self.dialogs
		release = threading.Event()
		self.useClient(FakeClient(answer="For A.", release=release))
		first = self._conversation()
		dialogs.state.current = first
		dialogs.startRequest("Question for A", first)
		second = self._conversation(("user", "B question"), ("assistant", "B answer"))
		dialogs.switchConversation(second)
		release.set()
		self.waitForRequests()
		self.assertIs(dialogs.state.current, second)
		self.assertEqual(second.lastAnswer, "B answer", "the displayed conversation is untouched")
		saved = self.conversations.ConversationStore(self.conversationsDir).load(first.id)
		self.assertEqual(saved.lastAnswer, "For A.", "the answer went into the conversation it was asked in")
		self.assertEqual(saved.history, [("user", "Question for A"), ("assistant", "For A.")])
		self.assertEqual(self.spoken[-1], "For A.")

	def test_errorIsStoredAndSpokenWhenDialogClosed(self):
		dialogs = self.dialogs
		error = self.codexClient.ProviderError(self.codexClient.KIND_NETWORK, "x", "ERROR: stream disconnected")
		self.useClient(FakeClient(error=error))
		conversation = self._conversation()
		dialogs.state.current = conversation
		dialogs.startRequest("Hello?", conversation)
		self.waitForRequests()
		self.assertEqual(self.spoken[-1], "Couldn't reach ChatGPT. Check your internet connection and try again.")
		self.assertEqual(conversation.messages[-1]["role"], "error")
		self.assertIn("Technical details: ERROR: stream disconnected", conversation.messages[-1]["text"])
		self.assertEqual(conversation.history, [], "a failed question is not sent back as context")

	def test_cancelRemovesTheQuestionAndIgnoresALateAnswer(self):
		dialogs = self.dialogs
		release = threading.Event()
		self.useClient(FakeClient(answer="late answer", release=release))
		conversation = self._conversation()
		dialogs.state.current = conversation
		pending = dialogs.startRequest("Never mind", conversation)
		self.assertEqual(len(conversation.messages), 1)
		self.assertIs(dialogs.cancelRequest(), pending)
		self.assertTrue(pending.cancelEvent.is_set())
		self.assertEqual(conversation.messages, [])
		count = len(self.spoken)
		release.set()
		time.sleep(0.2)
		self.assertEqual(len(self.spoken), count)
		self.assertEqual(conversation.lastAnswer, "")

	def test_projectModeAddsInstructionsAndReportsNewFiles(self):
		dialogs = self.dialogs
		project = os.path.join(self.tempDir, "project")
		os.makedirs(project)
		with open(os.path.join(project, "existing.txt"), "w") as f:
			f.write("old")

		def createFiles():
			with open(os.path.join(project, "report.docx"), "w") as f:
				f.write("new")
			with open(os.path.join(project, "existing.txt"), "w") as f:
				f.write("changed text")

		client = self.useClient(FakeClient(answer="I saved report.docx.", onAsk=createFiles))
		conversation = self._conversation(projectFolder=project)
		dialogs.state.current = conversation
		dialogs.startRequest("Write a report", conversation)
		self.waitForRequests()
		self.assertIn("The current working directory is the user's project folder.", client.prompts[0])
		self.assertEqual(self.spoken[-2:], ["I saved report.docx.", "Created: report.docx. Changed: existing.txt"])
		self.assertEqual(conversation.messages[-1], {"role": "note", "text": "Created: report.docx. Changed: existing.txt", "time": conversation.messages[-1]["time"]})
		self.assertEqual(conversation.history[-1], ("assistant", "I saved report.docx."), "notes are not context")

	def test_shutdownKillsRequestAndLoginAndRestoresSpeech(self):
		# C5 and feature 1: terminate() kills Codex and never leaves NVDA muted.
		dialogs = self.dialogs
		dictation = sys.modules["askAI.dictation"]
		release = threading.Event()
		client = self.useClient(FakeClient(release=release))
		conversation = self._conversation()
		dialogs.state.current = conversation
		pending = dialogs.startRequest("Long question", conversation)
		loginClient = mock.Mock()
		dialogs._loginClient = loginClient
		speech = FakeSpeech(mode="onDemand")
		session = dictation.DictationSession(dictation.SpeechModeAdapter(speech))
		session.start("")
		dialogs._activeDictation = session
		self.assertEqual(speech.mode, "off")
		plugin = self.package.GlobalPlugin()
		plugin.terminate()
		release.set()
		self.assertTrue(client.terminated)
		self.assertTrue(pending.cancelEvent.is_set())
		loginClient.cancelLogin.assert_called_once_with()
		self.assertIsNone(dialogs._loginClient)
		self.assertIsNone(dialogs.state.pending)
		self.assertEqual(speech.mode, "onDemand", "speech mode restored exactly")
		self.assertIsNone(dialogs._activeDictation)

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
		client.providerId = "codex"
		client.loginStatus.return_value = ("signedInSubscription", "Logged in using ChatGPT")
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

	def test_conversationExportBlocks(self):
		manager = importlib.import_module("askAI.conversationManager")
		conversation = self._conversation(("user", "Q?"), ("assistant", "**A**"), ("note", "Created: x.pdf"))
		self.assertEqual(
			manager.conversationBlocks(conversation),
			[("heading", "You"), ("text", "Q?"), ("heading", "ChatGPT"), ("text", "A"), ("text", "Created: x.pdf")],
		)
		self.assertEqual(manager.conversationBlocks(conversation, raw=True)[3], ("text", "**A**"))


class TestServicesWithFakeNvda(FakeNvdaTestCase):
	"""Ask AI 1.2: per-conversation service, Claude answers, migration from Ask GPT and the old add-on prompt."""

	def test_newConversationUsesTheDefaultService(self):
		self.conf.update(provider="claude", claudeModel="opus", claudeReasoningEffort="xhigh")
		conversation = self.dialogs.state.newConversation()
		self.assertEqual((conversation.provider, conversation.model, conversation.reasoningEffort), ("claude", "opus", "xhigh"))
		self.conf["provider"] = "unknown"
		self.assertEqual(self.dialogs.state.newConversation().provider, "codex")

	def test_makeClientFollowsTheConversationsService(self):
		self.conf.update(claudePath="C:\\claude.exe", codexPath="C:\\codex.cmd", allowProjectWrites=False)
		conversation = self._conversation(provider="claude", model="opus", reasoningEffort="max", projectFolder=self.tempDir)
		client = self.dialogs.makeClient(conversation=conversation)
		self.assertIsInstance(client, self.providers.ClaudeProvider)
		self.assertEqual((client.executablePath, client.model, client.reasoningEffort), ("C:\\claude.exe", "opus", "max"))
		self.assertEqual(client.projectFolder, self.tempDir)
		self.assertFalse(client.allowProjectWrites)
		codexClient = self.dialogs.makeClient(providerId="codex")
		self.assertIsInstance(codexClient, self.providers.CodexProvider)
		self.assertEqual(codexClient.executablePath, "C:\\codex.cmd")
		# An unknown effort for a Claude model falls back to the model's default.
		conversation.reasoningEffort = "ultra"
		self.assertEqual(self.dialogs.makeClient(conversation=conversation).reasoningEffort, "low")

	def test_claudeAnswerIsLabelledAndStoredWithItsService(self):
		dialogs = self.dialogs
		client = FakeClient(answer="**Bonjour**")
		client.providerId = "claude"
		self.useClient(client)
		conversation = self._conversation(provider="claude", projectFolder=self.tempDir)
		dialogs.state.current = conversation
		pending = dialogs.startRequest("Say hello", conversation)
		self.assertEqual(pending.providerId, "claude")
		self.waitForRequests()
		self.assertEqual(self.spoken[-1], "Bonjour")
		self.assertIn("cannot run commands", client.prompts[0], "Claude's project folder rules")
		saved = self.conversations.ConversationStore(self.conversationsDir).load(conversation.id)
		self.assertEqual(saved.provider, "claude")
		self.assertEqual(saved.messages[-1]["provider"], "claude")
		self.assertEqual(dialogs.transcriptEntries(saved)[-1], ("Claude: Bonjour", True))
		# Switching the conversation to ChatGPT keeps the earlier answer labelled Claude.
		saved.provider = "codex"
		saved.addMessage("assistant", "Hello")
		self.assertEqual(
			[text for text, isAnswer in dialogs.transcriptEntries(saved) if isAnswer],
			["Claude: Bonjour", "ChatGPT: Hello"],
		)
		manager = importlib.import_module("askAI.conversationManager")
		headings = [text for kind, text in manager.conversationBlocks(saved) if kind == "heading"]
		self.assertEqual(headings, ["You", "Claude", "ChatGPT"])

	def test_errorsNameTheService(self):
		base = self.codexClient
		error = base.ProviderError(base.KIND_NOT_LOGGED_IN, "x", "Not logged in", provider="claude")
		message = self.dialogs.friendlyError(error)
		self.assertIn("Sign in to Claude in NVDA Settings > Ask AI", message)
		self.assertIn("Claude Code", message)
		self.assertNotIn("ChatGPT", message)
		codexMessage = self.dialogs.friendlyError(base.ProviderError(base.KIND_NOT_LOGGED_IN, "x", provider="codex"))
		self.assertIn("Sign in to ChatGPT in NVDA Settings > Ask AI", codexMessage)
		self.assertEqual(
			self.dialogs.friendlyError(base.ProviderError(base.KIND_NETWORK, "", provider="claude")),
			"Couldn't reach Claude. Check your internet connection and try again.",
		)
		notInstalled = self.dialogs.friendlyError(base.ProviderError(base.KIND_NOT_INSTALLED, "", provider="claude"))
		self.assertIn("irm https://claude.ai/install.ps1 | iex", notInstalled)
		limit = base.ProviderError(base.KIND_USAGE_LIMIT, "x", provider="claude", resetText="3pm (Europe/London)")
		self.assertEqual(
			self.dialogs.friendlyError(limit),
			"You have reached the usage limit of your Claude plan. It resets 3pm (Europe/London).",
		)
		moment = time.mktime(time.localtime(time.time() + 3600))
		limit = base.ProviderError(base.KIND_USAGE_LIMIT, "x", provider="claude", resetEpoch=moment)
		self.assertIn("It resets at %s." % time.strftime("%H:%M", time.localtime(moment)), self.dialogs.friendlyError(limit))
		busy = base.ProviderError(base.KIND_OTHER, "x", reason=base.REASON_BUSY, provider="claude")
		self.assertEqual(self.dialogs.friendlyError(busy), "Claude is busy right now. Please try again in a moment.")
		outdated = base.ProviderError(base.KIND_CONFIG, "x", reason=base.REASON_OUTDATED, provider="claude")
		self.assertIn("claude update", self.dialogs.friendlyError(outdated))
		self.assertEqual(self.dialogs.shortStatusForError(base.ProviderError(base.KIND_NOT_INSTALLED, "", provider="claude")), "Claude Code not found")

	def test_statusTexts(self):
		base = self.codexClient
		self.assertEqual(self.dialogs.statusText("claude", base.STATE_SUBSCRIPTION), "Signed in with Claude")
		self.assertEqual(self.dialogs.statusText("codex", base.STATE_SUBSCRIPTION), "Signed in with ChatGPT")
		self.assertIn("API key", self.dialogs.statusText("claude", base.STATE_API_KEY))
		self.assertEqual(self.dialogs.statusText("claude", base.STATE_SIGNED_OUT), "Not signed in")

	def test_finishLoginForClaude(self):
		dialogs = self.dialogs
		client = mock.Mock()
		client.providerId = "claude"
		client.loginStatus.return_value = ("signedInApiKey", "Signed in with api_key")
		dialogs._loginClient = client
		dialogs._finishLogin(client, timedOut=False)
		self.waitUntil(lambda: self.spoken)
		self.assertEqual(self.spoken, ["Claude Code is signed in with an API key, not with your subscription."])

	def _migrate(self, oldSection, newSection=None):
		package = self.package
		self.conf["migratedFromAskGPT"] = False
		profile = {"askGPT": oldSection}
		if newSection is not None:
			profile["askAI"] = newSection
		self.fakeModules["config"].conf.profiles = [profile]
		package.migrateFromAskGPT()
		return package

	def test_migrationCopiesOldSettingsOnce(self):
		self._migrate({"codexPath": "C:\\tools\\codex.cmd", "timeout": "300", "speakAnswers": "False", "model": "gpt-x"})
		self.assertEqual(
			(self.conf["codexPath"], self.conf["timeout"], self.conf["speakAnswers"], self.conf["model"]),
			("C:\\tools\\codex.cmd", 300, False, "gpt-x"),
		)
		self.assertTrue(self.conf["migratedFromAskGPT"])
		self.conf["timeout"] = 60
		self.package.migrateFromAskGPT()
		self.assertEqual(self.conf["timeout"], 60, "runs only once")

	def test_migrationKeepsExistingAskAISettings(self):
		self._migrate({"timeout": "300"}, newSection={"timeout": "90"})
		self.assertEqual(self.conf["timeout"], 180)
		self.assertTrue(self.conf["migratedFromAskGPT"])

	def test_migrationCopiesConversations(self):
		old = os.path.join(self.tempDir, "askGPT", "conversations")
		new = os.path.join(self.tempDir, "askAI", "conversations")
		os.makedirs(old)
		with open(os.path.join(old, "a" * 32 + ".json"), "w", encoding="utf-8") as f:
			json.dump({"schemaVersion": 1, "id": "a" * 32, "title": "Old", "messages": [{"role": "assistant", "text": "x"}]}, f)
		self.conf["conversationsDir"] = ""
		migration = sys.modules["askAI.migration"]
		with mock.patch.object(migration, "oldConversationsDir", lambda: old), mock.patch.object(
			self.conversations,
			"defaultConversationsDir",
			lambda: new,
		):
			self._migrate({})
		self.assertTrue(os.path.exists(os.path.join(old, "a" * 32 + ".json")), "copied, not moved")
		loaded = self.conversations.ConversationStore(new).load("a" * 32)
		self.assertEqual((loaded.title, loaded.provider, loaded.messages[0]["provider"]), ("Old", "codex", "codex"))

	def test_offerToRemoveOldAddon(self):
		package = self.package
		gui = self.fakeModules["gui"]
		gui.mainFrame.prePopup = gui.mainFrame.postPopup = lambda: None
		old = mock.Mock()
		old.name, old.isEnabled, old.isPendingRemove = "askGPT", True, False
		self.fakeModules["addonHandler"].getAvailableAddons = lambda: iter([old])
		self.conf["askedToRemoveAskGPT"] = False
		questions = []
		with mock.patch.object(self.dialogs, "askYesNo", lambda message, title, parent=None: questions.append(message) or False):
			package.offerToRemoveOldAddon()
			package.offerToRemoveOldAddon()
		self.assertEqual(questions, ["The old Ask GPT add-on is still installed. Remove it now? (Restart NVDA afterwards.)"])
		old.requestRemove.assert_not_called()
		self.assertTrue(self.conf["askedToRemoveAskGPT"], "never asked again after No")
		self.conf["askedToRemoveAskGPT"] = False
		with mock.patch.object(self.dialogs, "askYesNo", lambda *a, **k: True):
			package.offerToRemoveOldAddon()
		old.requestRemove.assert_called_once_with()

	def test_noPromptWithoutTheOldAddon(self):
		self.fakeModules["addonHandler"].getAvailableAddons = lambda: iter([])
		self.conf["askedToRemoveAskGPT"] = False
		with mock.patch.object(self.dialogs, "askYesNo", side_effect=AssertionError("asked")):
			self.package.offerToRemoveOldAddon()
		self.assertFalse(self.conf["askedToRemoveAskGPT"])


class FakeSpeech:
	"""Minimal stand-in for NVDA's speech module (speech.getState().speechMode, setSpeechMode, SpeechMode)."""

	class SpeechMode:
		off = "off"
		beeps = "beeps"
		talk = "talk"
		onDemand = "onDemand"

	def __init__(self, mode="talk"):
		self.mode = mode
		self.history = []

	def getState(self):
		return types.SimpleNamespace(speechMode=self.mode)

	def setSpeechMode(self, mode):
		self.history.append(mode)
		self.mode = mode


class TestAskYesNo(unittest.TestCase):
	"""Fix round 3, item 5: Yes and No only, Escape means No; gui.messageBox on NVDA 2024.x."""

	def setUp(self):
		self.fake, _spoken = buildFakeNvda(os.path.join(helpers.ROOT, "tmp-config"))
		self.saved = {name: sys.modules.get(name) for name in list(self.fake) + ["gui.message"]}
		sys.modules.update(self.fake)
		sys.path.insert(0, os.path.join(helpers.ROOT, "addon", "globalPlugins"))
		for name in list(sys.modules):
			if name == "askAI" or name.startswith("askAI."):
				del sys.modules[name]
		self.dialogs = importlib.import_module("askAI.dialogs")

	def tearDown(self):
		for name in list(sys.modules):
			if name == "askAI" or name.startswith("askAI."):
				del sys.modules[name]
		sys.path.remove(os.path.join(helpers.ROOT, "addon", "globalPlugins"))
		for name, module in self.saved.items():
			if module is None:
				sys.modules.pop(name, None)
			else:
				sys.modules[name] = module

	def _installMessageModule(self, result):
		created = []

		class ReturnCode:
			YES, NO, CANCEL = 1, 2, 3

		class DefaultButtonSet:
			YES_NO = ("yes", "no")
			YES_NO_CANCEL = ("yes", "no", "cancel")

		class MessageDialog:
			def __init__(self, parent, message, title, dialogType=None, *, buttons=None):
				self.args = (parent, message, title)
				self.buttons = buttons
				self.escapeId = None
				created.append(self)

			def SetEscapeId(self, id):
				self.escapeId = id
				return self

			def ShowModal(self):
				# Escape returns the escape id.
				return self.escapeId if result == "escape" else getattr(ReturnCode, result)

			@classmethod
			def ask(cls, *args, **kwargs):
				raise AssertionError("ask() adds a Cancel button")

		sys.modules["gui.message"] = _module(
			"gui.message",
			MessageDialog=MessageDialog,
			ReturnCode=ReturnCode,
			DefaultButtonSet=DefaultButtonSet,
		)
		return created, ReturnCode

	def test_yesNoOnly(self):
		created, ReturnCode = self._installMessageModule("YES")
		self.assertTrue(self.dialogs.askYesNo("Sign out?", "Ask AI", None))
		dialog = created[-1]
		self.assertEqual(dialog.buttons, ("yes", "no"))
		self.assertEqual(dialog.args, (None, "Sign out?", "Ask AI"))
		self.assertEqual(dialog.escapeId, ReturnCode.NO)

	def test_noAndEscapeMeanNo(self):
		self._installMessageModule("NO")
		self.assertFalse(self.dialogs.askYesNo("q", "t"))
		self._installMessageModule("escape")
		self.assertFalse(self.dialogs.askYesNo("q", "t"))

	def test_fallbackForNvda2024(self):
		sys.modules["gui.message"] = None  # Makes "from gui.message import ..." raise ImportError.
		calls = []
		wx = self.fake["wx"]
		wx.YES, wx.YES_NO, wx.ICON_QUESTION = 2, 10, 256
		self.fake["gui"].messageBox = lambda *args: calls.append(args) or 2
		self.assertTrue(self.dialogs.askYesNo("q", "t", "parent"))
		self.assertEqual(calls, [("q", "t", 10 | 256, "parent")])


class TestSettingsStatus(FakeNvdaTestCase):
	"""Fix round 3, item 1: the first status result is announced while the panel is shown."""

	def _fakePanel(self, focused=False, shown=True):
		panel = mock.MagicMock()
		panel.__bool__.return_value = True
		panel._statusGeneration = 3
		panel._initialStatusPending = True
		panel.FindFocus.return_value = panel.statusCtrl if focused else object()
		panel.IsShownOnScreen.return_value = shown
		return panel

	def setStatus(self, panel, text, announce=False, generation=3):
		self.dialogs.AskAISettingsPanel._setStatus(panel, generation, text, announce)

	def test_firstResultAnnouncedOnce(self):
		panel = self._fakePanel()
		self.setStatus(panel, "Signed in with ChatGPT")
		panel.statusCtrl.SetValue.assert_called_with("Signed in with ChatGPT")
		self.assertEqual(self.spoken, ["Ask AI: Signed in with ChatGPT"])
		self.setStatus(panel, "Not signed in")
		self.assertEqual(self.spoken, ["Ask AI: Signed in with ChatGPT"], "later silent refreshes stay silent")

	def test_notAnnouncedWhenPanelHidden(self):
		panel = self._fakePanel(shown=False)
		self.setStatus(panel, "Signed in with ChatGPT")
		self.assertEqual(self.spoken, [])
		self.assertFalse(panel._initialStatusPending)

	def test_focusedOrRequestedAnnouncesPlainText(self):
		panel = self._fakePanel(focused=True)
		self.setStatus(panel, "Not signed in")
		self.assertEqual(self.spoken, ["Not signed in"])
		panel = self._fakePanel()
		self.setStatus(panel, "Signed out", announce=True)
		self.assertEqual(self.spoken[-1], "Signed out")

	def test_staleResultIgnored(self):
		panel = self._fakePanel()
		self.setStatus(panel, "old", generation=2)
		panel.statusCtrl.SetValue.assert_not_called()
		self.assertEqual(self.spoken, [])


class TestSettingsLayoutSource(unittest.TestCase):
	"""Fix round 3, items 1 and 2: checks on the panel code (the real layout needs wx on Windows)."""

	def setUp(self):
		with open(os.path.join(helpers.PACKAGE_DIR, "dialogs.py"), encoding="utf-8") as f:
			source = f.read()
		start = source.index("class AskAISettingsPanel(")
		self.panel = source[start:source.index("\ndef shortStatusForError")]
		self.source = source

	def test_statusIsTabbableAndFirst(self):
		self.assertIn("STATUS_STYLE = wx.TE_READONLY | wx.TE_MULTILINE | wx.TE_NO_VSCROLL", self.source)
		makeSettings = self.panel[self.panel.index("def makeSettings"):]
		self.assertIn("style=STATUS_STYLE", makeSettings)
		firstControl = re.search(r"(addLabeledControl|wx\.TextCtrl\(|addButton\(|wx\.Button\(|wx\.CheckBox\()", makeSettings)
		self.assertEqual(firstControl.group(1), "addLabeledControl")
		self.assertLess(makeSettings.index("self.statusCtrl ="), makeSettings.index("addButton("))

	def test_widthsFitTheSettingsArea(self):
		import ast

		constants = {}
		for node in ast.parse(self.source).body:
			if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
				constants[node.targets[0].id] = node.value.value
		self.assertLessEqual(constants["PANEL_CONTENT_WIDTH"], 544)
		# Each row: its fixed-width control plus room for a label or a button stays inside about 540 pixels.
		self.assertLessEqual(constants["STATUS_WIDTH"] + 80, 540)
		self.assertLessEqual(constants["PATH_MIN_WIDTH"] + 120, 540)
		self.assertLessEqual(constants["INSTRUCTIONS_MIN_WIDTH"], 540)
		# No literal pixel widths in the panel, and the long path label is not beside the field.
		self.assertIsNone(re.search(r"size=\(\d{3}", self.panel))
		self.assertNotIn("orientation=wx.HORIZONTAL", self.panel)
		# Long labels sit on their own line above their field: the path, conversations folder and model.
		self.assertEqual(self.panel.count("self._labelledPathRow(\n"), 2)
		self.assertIn("modelBlock.Add(modelLabel)", self.panel)
		labels = [
			label
			for label in re.findall(r'_\(\s*"((?:[^"\\]|\\.)*)"\s*\)', self.panel)
			if "&" in label or label.endswith(":")
		]
		self.assertGreaterEqual(len(labels), 10)
		for label in labels:
			self.assertLessEqual(len(label), 70, label)


def _mnemonics(source, className):
	"""Letters after "&" in the translatable labels of one class in a module's source."""
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
		letters = _mnemonics(self.source, "AskAISettingsPanel")
		self.assertGreaterEqual(len(letters), 20)
		used = [letter for letter, label in letters]
		self.assertEqual(len(used), len(set(used)), letters)
		for letter, label in letters:
			self.assertNotIn(letter, "oca", label)
		self.assertEqual(sorted(used), sorted("vgshtyxbmripkwjndfelu"), letters)
		labels = [label for letter, label in letters]
		for label in (
			"Default AI ser&vice:",
			"Account settin&gs for:",
			"&Sign in to {service}...",
			"Sign ou&t",
			"S&peak answers automatically",
			"Cop&y sign-in link",
			"{program} e&xecutable path (blank to detect automatically):",
			"Check for updates automatica&lly",
			"Check for &updates now",
		):
			self.assertIn(label, labels)

	def test_askDialogMnemonicsAreUnique(self):
		letters = _mnemonics(self.source, "AskAIDialog")
		used = [letter for letter, label in letters if label != "&Ask"]  # &Ask and &Cancel share one button.
		self.assertEqual(len(used), len(set(used)), letters)
		labels = [label for letter, label in letters]
		self.assertEqual(labels[0], "&Question:", "the Question box comes first")
		# Feature 5: every new control has its own letter.
		self.assertEqual(sorted(used), sorted("qcdvowsmejfxlrn"), letters)
		for label in (
			"Save ans&wer as...",
			"AI &service:",
			"&Model:",
			"Reasoning &effort:",
			"Pro&ject folder:",
			"Choose project &folder...",
			"Open folder in E&xplorer",
			"C&lear project folder",
			"Conve&rsations...",
			"&New conversation",
		):
			self.assertIn(label, labels)
		# The service choice comes right before the model choice.
		self.assertEqual(labels[labels.index("AI &service:") + 1], "&Model:")

	def test_settingsPanelHasTheNewSettings(self):
		labels = [label for letter, label in _mnemonics(self.source, "AskAISettingsPanel")]
		for label in (
			"Allow the AI to create and edit files in the pro&ject folder",
			"&Dictation silence timeout in seconds:",
			"Conversations &folder (blank for the default):",
			"Choos&e folder...",
			"Default &model (blank uses your plan's default):",
		):
			self.assertIn(label, labels)
		# Old hard-coded model presets are gone; the list comes from Codex's own model cache.
		self.assertNotIn('"gpt-5-codex"', self.source)

	def test_conversationManagerMnemonicsAreUnique(self):
		with open(os.path.join(helpers.PACKAGE_DIR, "conversationManager.py"), encoding="utf-8") as f:
			source = f.read()
		letters = _mnemonics(source, "ConversationManagerDialog")
		# "&Archive" and "Un&archive" are the same button with a flipped label (set in two places).
		used = [
			letter for label, letter in {label: letter for letter, label in letters}.items() if label != "Un&archive"
		]
		self.assertEqual(len(used), len(set(used)), letters)
		self.assertEqual(sorted(used), sorted("vhcradxi"), letters)


if __name__ == "__main__":
	unittest.main()
