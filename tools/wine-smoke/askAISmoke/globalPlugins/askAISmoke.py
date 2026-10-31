# -*- coding: UTF-8 -*-
# Throwaway QA plugin: drives the real Ask AI add-on inside a running NVDA (run under Wine by
# tools/wine-smoke/run.sh), logs "ASKAI-SMOKE PASS|FAIL|INFO ..." lines to nvda.log, then exits NVDA.
# Expects codexPath = C:\fakecodex\codex.cmd (the fake Codex, migrated from the [askGPT] section) and sets
# claudePath = C:\fakeclaude\claude.cmd (the fake Claude Code). Never install for real use.
# The steps run as a generator on the main thread; each yield is a delay in ms before the next step,
# so modal dialogs and the add-on's own wx.CallAfter work keep running in between.

import glob
import os
import sys
import time

import wx

import api
import buildVersion
import config
import core
import globalPluginHandler
import globalVars
import gui
import inputCore
import keyboardHandler
import scriptHandler
import tones
import ui
from gui.settingsDialogs import NVDASettingsDialog
from logHandler import log

FAKE_DIR = "C:\\fakecodex"
FAKE_CODEX = FAKE_DIR + "\\codex.cmd"
FAKE_CLAUDE_DIR = "C:\\fakeclaude"
FAKE_CLAUDE = FAKE_CLAUDE_DIR + "\\claude.cmd"
FILES = "C:\\smokefiles"
CLAUDE_ANSWER_SPOKEN = "Bonjour from Claude."
MIGRATED_ID = "0123456789abcdef0123456789abcdef"
START_DELAY_MS = 5000
TICK_MS = 200
WATCHDOG_SECONDS = 680
QUESTION = "What is the capital of France?"
ANSWER_RAW = "**Paris** is the capital of France."
ANSWER_SPOKEN = "Paris is the capital of France."
SLOW_QUESTION = "SLOW and of Germany?"
PROJECT = "C:\\smokeproject"

counts = {"PASS": 0, "FAIL": 0}
spoken = []
spokenModes = []  # (text, speech mode name) for each ui.message, to check speech was back on.
beeps = []
speeches = []  # Strings passed to speech.speak (speakThen bypasses ui.message).
sent = []  # Keys sent with KeyboardInputGesture.send (Dictate sends windows+h).


def report(ok, name, detail=""):
	tag = "PASS" if ok else "FAIL"
	counts[tag] += 1
	log.info("ASKAI-SMOKE %s %s%s" % (tag, name, (" | %s" % detail) if detail else ""))
	return ok


def info(text):
	log.info("ASKAI-SMOKE INFO %s" % text)


def phase(name):
	"""Tell run.sh to take a screenshot now."""
	info("phase %s" % name)
	try:
		with open(os.path.join(FAKE_DIR, "phase.txt"), "w") as f:
			f.write(name)
	except OSError:
		pass


def readFake(name, folder=FAKE_DIR):
	try:
		with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
			return f.read()
	except OSError:
		return ""


def installSpies():
	originalMessage = ui.message

	def messageSpy(text, *args, **kwargs):
		spoken.append(text)
		try:
			import speech

			mode = speech.getState().speechMode.name
		except Exception:
			mode = "?"
		spokenModes.append((text, mode))
		info("ui.message %r (speech mode %s)" % (text, mode))
		return originalMessage(text, *args, **kwargs)

	ui.message = messageSpy
	originalBeep = tones.beep

	def beepSpy(*args, **kwargs):
		beeps.append(args)
		return originalBeep(*args, **kwargs)

	tones.beep = beepSpy

	import speech

	originalSpeak = speech.speak

	def speakSpy(sequence, *args, **kwargs):
		speeches.extend(item for item in sequence if isinstance(item, str))
		return originalSpeak(sequence, *args, **kwargs)

	speech.speak = speakSpy
	originalSend = keyboardHandler.KeyboardInputGesture.send

	def sendSpy(gesture):
		name = gesture.identifiers[-1].split(":")[-1] if gesture.identifiers else repr(gesture)
		sent.append(name)
		info("KeyboardInputGesture.send %r" % (gesture.identifiers,))
		if "windows" in name and name.endswith("h"):
			# Wine has no voice typing: a real Windows+H would just type "h". Record it, like Windows would act on it.
			return None
		return originalSend(gesture)

	keyboardHandler.KeyboardInputGesture.send = sendSpy


def findMessageDialog():
	"""A shown NVDA message dialog (gui.message.MessageDialog in 2025.1+, else wx.MessageDialog-like)."""
	for window in wx.GetTopLevelWindows():
		if window and window.IsShown() and type(window).__name__ in ("MessageDialog", "GenericMessageDialog"):
			return window
	return None


def walk(window):
	for child in window.GetChildren():
		yield child
		yield from walk(child)


def msaa(window):
	"""What NVDA itself gets from MSAA for this window (Wine's oleacc/user32 here, not real Windows)."""
	try:
		import winUser
		from NVDAObjects.IAccessible import getNVDAObjectFromEvent

		obj = getNVDAObjectFromEvent(window.GetHandle(), winUser.OBJID_CLIENT, 0)
		if obj is None:
			return "msaa=None"
		return "msaaName=%r msaaRole=%s" % (obj.name, obj.role.name)
	except Exception as e:
		return "msaaError=%r" % (e,)


def describe(window):
	label = window.GetLabel() if hasattr(window, "GetLabel") else ""
	return "%s label=%r wxName=%r enabled=%s shown=%s keyboardFocusable=%s %s" % (
		type(window).__name__,
		label,
		window.GetName(),
		window.IsEnabled(),
		window.IsShown(),
		window.AcceptsFocusFromKeyboard(),
		msaa(window),
	)


def mnemonics(window):
	"""Mnemonic letter -> labels, for shown controls under window."""
	found = {}
	for child in walk(window):
		if not child.IsShown() or not hasattr(child, "GetLabel"):
			continue
		label = child.GetLabel().replace("&&", "")
		index = label.find("&")
		if index >= 0 and index + 1 < len(label):
			found.setdefault(label[index + 1].lower(), []).append(label)
	return found


def press(button):
	"""Post a button click to the event queue (never blocks the step generator)."""
	event = wx.CommandEvent(wx.wxEVT_BUTTON, button.GetId())
	event.SetEventObject(button)
	wx.PostEvent(button, event)


def charHook(dialog, key, target):
	"""Fallback when synthetic key input does not arrive: deliver EVT_CHAR_HOOK directly."""
	event = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
	event.SetKeyCode(key)
	event.SetEventObject(target)
	dialog.GetEventHandler().ProcessEvent(event)


def findTopLevelOfType(typeName, title=None):
	for window in wx.GetTopLevelWindows():
		if window and window.IsShown() and type(window).__name__ == typeName and (title is None or window.GetTitle() == title):
			return window
	return None


def findButton(window, label):
	return next((w for w in walk(window) if isinstance(w, wx.Button) and w.GetLabel() == label), None)


def choose(choice, index):
	"""Select a wx.Choice item and send EVT_CHOICE, as arrowing to it would."""
	choice.SetSelection(index)
	event = wx.CommandEvent(wx.wxEVT_CHOICE, choice.GetId())
	event.SetEventObject(choice)
	event.SetInt(index)
	choice.GetEventHandler().ProcessEvent(event)


def choiceItems(choice):
	return [choice.GetString(i) for i in range(choice.GetCount())]


class FakeDirDialog:
	"""Stands in for wx.DirDialog (the native folder picker cannot be driven under Wine)."""

	path = PROJECT

	def __init__(self, *args, **kwargs):
		pass

	def __enter__(self):
		return self

	def __exit__(self, *args):
		return False

	def ShowModal(self):
		return wx.ID_OK

	def GetPath(self):
		return self.path


class FakeFileDialog(FakeDirDialog):
	paths = []

	def GetPaths(self):
		return list(self.paths)


def findSettingsDialog():
	for window in wx.GetTopLevelWindows():
		if isinstance(window, NVDASettingsDialog) and window:
			return window
	return None


def findTopLevel(title):
	for window in wx.GetTopLevelWindows():
		if window and window.IsShown() and window.GetTitle() == title:
			return window
	return None


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._deadline = time.monotonic() + WATCHDOG_SECONDS
		self._steps = self._run()
		self._finished = False
		self.sim = wx.UIActionSimulator()
		wx.CallLater(START_DELAY_MS, self._advance)

	def _advance(self):
		if self._finished:
			return
		if time.monotonic() > self._deadline:
			report(False, "watchdog", "the smoke test did not finish in %d s" % WATCHDOG_SECONDS)
			self._exit()
			return
		try:
			delay = next(self._steps)
		except StopIteration:
			return
		except Exception:
			counts["FAIL"] += 1
			log.error("ASKAI-SMOKE FAIL unexpected exception in the smoke test", exc_info=True)
			self._exit()
			return
		wx.CallLater(delay, self._advance)

	def _exit(self):
		self._finished = True
		log.info("ASKAI-SMOKE SUMMARY pass=%d fail=%d" % (counts["PASS"], counts["FAIL"]))
		phase("exit")
		wx.CallLater(1500, core.triggerNVDAExit)

	@staticmethod
	def _waitFor(condition, timeoutMs):
		end = time.monotonic() + timeoutMs / 1000.0
		while True:
			try:
				value = condition()
			except Exception:
				value = None
			if value or time.monotonic() > end:
				return value
			yield TICK_MS

	def _gesture(self, gesture):
		"""Run a gesture through NVDA's input manager. NVDA refuses gestures while its watchdog recovers
		from a freeze (seen under Wine in oleacc focus handling), so wait and retry."""
		import watchdog

		for attempt in range(10):
			yield from self._waitFor(lambda: not watchdog.isAttemptingRecovery, 5000)
			try:
				inputCore.manager.executeGesture(gesture)
				info("gesture %s executed by inputCore (attempt %d)" % (gesture.identifiers[0], attempt + 1))
				return
			except inputCore.NoInputGestureAction:
				yield 500
		info("gesture %s refused 10 times by inputCore" % gesture.identifiers[0])

	def _key(self, dialog, key, target, effect, name):
		"""Press key with real synthetic input; fall back to EVT_CHAR_HOOK. Returns the path used."""
		self.sim.Char(key)
		ok = yield from self._waitFor(effect, 2500)
		if ok:
			info("%s delivered by wx.UIActionSimulator (SendInput)" % name)
			return "SendInput"
		info("%s not seen after SendInput; falling back to EVT_CHAR_HOOK" % name)
		charHook(dialog, key, target)
		yield from self._waitFor(effect, 2500)
		return "EVT_CHAR_HOOK"

	def _ask(self, dialog, dialogs, question):
		dialog.questionCtrl.SetValue(question)
		dialog.questionCtrl.SetInsertionPointEnd()
		dialog.questionCtrl.SetFocus()
		yield 200
		yield from self._key(dialog, wx.WXK_RETURN, dialog.questionCtrl,
			lambda: dialogs.state.pending is not None or dialog.questionCtrl.GetValue() == "", "Enter (%s)" % question)
		yield from self._waitFor(lambda: dialogs.state.pending is None, 20000)
		yield 500

	def _saveAs(self, dialog, formatIndex, name, title):
		"""Drive the real format chooser and file name prompt. Returns the suggested name."""
		chooser = yield from self._waitFor(lambda: findTopLevelOfType("SingleChoiceDialog", title), 5000)
		if chooser is None:
			return None
		info("format choices: %r" % [chooser.GetStringSelection()])
		chooser.SetSelection(formatIndex)
		chooser.EndModal(wx.ID_OK)
		entry = yield from self._waitFor(lambda: findTopLevelOfType("TextEntryDialog", title), 5000)
		if entry is None:
			return None
		suggested = entry.GetValue()
		entry.SetValue(name)
		entry.EndModal(wx.ID_OK)
		return suggested

	def _newFeatures(self, dialogs, store, firstId, mediumId):
		import zipfile

		yield from self._gesture(keyboardHandler.KeyboardInputGesture.fromName("NVDA+alt+g"))
		dialog = yield from self._waitFor(dialogs.AskAIDialog.getInstance, 5000)
		if not report(dialog is not None, "Ask dialog reopened for the new features"):
			return
		yield 800
		report(dialogs.state.current.id == mediumId, "reopened dialog continues the newest conversation")

		# Feature 3: model and reasoning effort per conversation, from Codex's model list.
		items = choiceItems(dialog.modelChoice)
		report(items[:1] == ["Default (your plan's default)"] and "GPT Test Fast" in items and "GPT Test Deep" in items
			and "Hidden" not in items, "model choice lists Default and Codex's listed models", repr(items))
		spoken.clear()
		choose(dialog.modelChoice, items.index("GPT Test Fast"))
		yield from self._waitFor(lambda: "Model changed to GPT Test Fast" in spoken, 3000)
		report("Model changed to GPT Test Fast" in spoken, "model change announced", repr(spoken))
		efforts = choiceItems(dialog.effortChoice)
		report(efforts == ["Low (fastest)", "Medium", "High", "Extra high"], "effort choice lists the model's levels", repr(efforts))
		choose(dialog.effortChoice, efforts.index("Extra high"))
		current = dialogs.state.current
		report((current.model, current.reasoningEffort) == ("gpt-test-fast", "xhigh"), "model and effort stored with the conversation",
			repr((current.model, current.reasoningEffort)))
		yield from self._ask(dialog, dialogs, "Which model?")
		calls = readFake("calls.log")
		lastCall = calls[calls.rfind("ARGS: \"exec\""):]
		report('"-m" "gpt-test-fast"' in lastCall and '"model_reasoning_effort=xhigh"' in lastCall,
			"Codex runs with the conversation's model and effort", repr(lastCall[:400]))
		choose(dialog.modelChoice, choiceItems(dialog.modelChoice).index("GPT Test Deep"))
		report(choiceItems(dialog.effortChoice) == ["Medium", "High"] and dialogs.state.current.reasoningEffort == "medium",
			"an unsupported effort falls back to the model's default", repr((choiceItems(dialog.effortChoice), dialogs.state.current.reasoningEffort)))
		choose(dialog.modelChoice, 0)
		saved = store.load(mediumId)
		report(saved is not None and saved.model == "", "model change saved with the conversation", repr(saved.model if saved else None))

		# Feature 2: project folder, workspace-write sandbox, new-file detection.
		spoken.clear()
		original = wx.DirDialog
		wx.DirDialog = FakeDirDialog
		try:
			press(dialog.chooseProjectButton)
			yield from self._waitFor(lambda: "Project folder: smokeproject" in spoken, 3000)
		finally:
			wx.DirDialog = original
		report("Project folder: smokeproject" in spoken, "choosing the project folder announces its name", repr(spoken))
		report(dialog.projectCtrl.GetValue() == PROJECT and dialog.openProjectButton.IsEnabled() and dialog.clearProjectButton.IsEnabled(),
			"project folder shown, Open and Clear enabled", repr(dialog.projectCtrl.GetValue()))
		spoken.clear()
		yield from self._ask(dialog, dialogs, "MAKEFILE please")
		calls = readFake("calls.log")
		lastCall = calls[calls.rfind("ARGS: \"exec\""):]
		report('"--sandbox" "workspace-write"' in lastCall and '"-C" "%s"' % PROJECT in lastCall
			and "sandbox_workspace_write.network_access=false" in lastCall and "danger" not in lastCall,
			"project mode runs Codex in the folder with workspace-write and no network", repr(lastCall[:600]))
		report(("CWD: " + PROJECT).lower() in lastCall.lower(), "Codex's working directory is the project folder")
		stdin = readFake("last-stdin.txt")
		report("The current working directory is the user's project folder." in stdin, "project instructions are in the prompt")
		report(os.path.exists(os.path.join(PROJECT, "smoke-report.txt")), "the fake Codex created a file in the project")
		report("Created: smoke-report.txt" in spoken and "Created: smoke-report.txt" in dialog.conversationCtrl.GetValue(),
			"new file announced and listed in the Conversation box", repr(spoken))
		report(not os.path.exists(os.path.join(PROJECT, "answer")) and not any(n.startswith("answer-") for n in os.listdir(PROJECT)),
			"the answer file is not written into the project")

		# Save answer as Word into the project folder, twice (never overwrites).
		spoken.clear()
		press(dialog.saveButton)
		suggested = yield from self._saveAs(dialog, 0, "Smoke answer", "Save answer as")
		report(suggested == "MAKEFILE please", "file name suggested from the question", repr(suggested))
		yield from self._waitFor(lambda: any(str(s).startswith("Saved Smoke answer.docx") for s in spoken), 5000)
		docx = os.path.join(PROJECT, "Smoke answer.docx")
		report(("Saved Smoke answer.docx in %s" % PROJECT) in spoken, "save announced with file and folder", repr(spoken))
		try:
			with zipfile.ZipFile(docx) as package:
				document = package.read("word/document.xml").decode("utf-8")
			report(ANSWER_SPOKEN in document and "**" not in document, "saved .docx contains the answer", document[:200])
		except Exception as e:
			report(False, "saved .docx contains the answer", repr(e))
		press(dialog.saveButton)
		yield from self._saveAs(dialog, 0, "Smoke answer", "Save answer as")
		yield from self._waitFor(lambda: os.path.exists(os.path.join(PROJECT, "Smoke answer (2).docx")), 5000)
		report(os.path.exists(os.path.join(PROJECT, "Smoke answer (2).docx")), "saving again makes Smoke answer (2).docx")

		# Clear the project folder; saving then goes to Documents\Ask AI (PDF).
		spoken.clear()
		press(dialog.clearProjectButton)
		yield from self._waitFor(lambda: "Project folder cleared" in spoken, 3000)
		report(dialog.projectCtrl.GetValue() == "None" and not dialog.clearProjectButton.IsEnabled()
			and wx.Window.FindFocus() is dialog.chooseProjectButton, "clearing the project moves focus to Choose project folder",
			repr(spoken))
		documents = dialogs.documentsFolder()
		spoken.clear()
		press(dialog.saveButton)
		yield from self._saveAs(dialog, 1, "Smoke pdf", "Save answer as")
		pdf = os.path.join(documents, "Smoke pdf.pdf")
		yield from self._waitFor(lambda: os.path.exists(pdf), 5000)
		try:
			with open(pdf, "rb") as f:
				data = f.read()
			report(data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF") and b"Paris is the capital" in data,
				"PDF saved in Documents\\Ask AI", pdf)
		except OSError as e:
			report(False, "PDF saved in Documents\\Ask AI", repr(e))

		# Feature 4: conversation manager.
		spoken.clear()
		press(findButton(dialog, "Conve&rsations..."))
		manager = yield from self._waitFor(lambda: findTopLevelOfType("ConversationManagerDialog"), 5000)
		if not report(manager is not None, "Conversations button opens the conversation manager"):
			return
		yield 800
		phase("conversation-manager")
		yield 500
		listCtrl = manager.listCtrl
		titles = [listCtrl.GetItemText(i) for i in range(listCtrl.GetItemCount())]
		info("conversation list: %r" % titles)
		report(len(titles) >= 2 and QUESTION in titles, "conversation list shows the saved conversations", repr(titles))
		report(wx.Window.FindFocus() is listCtrl, "focus starts on the conversation list")
		selected = listCtrl.GetFirstSelected()
		report(selected >= 0 and manager._items[selected].id == mediumId, "the current conversation is selected")
		report(any(str(s).endswith("conversations") for s in spoken), "conversation count announced", repr(spoken))
		dupes = {k: v for k, v in mnemonics(manager).items() if len(v) > 1}
		report(not dupes, "conversation manager mnemonics unique", repr(mnemonics(manager)))
		report(listCtrl.GetColumnCount() == 5, "list has Title, Updated, Messages, Project and Service columns")

		# Rename the current conversation.
		press(manager.renameButton)
		entry = yield from self._waitFor(lambda: findTopLevelOfType("TextEntryDialog", "Rename conversation"), 5000)
		if entry is not None:
			entry.SetValue("Smoke renamed")
			entry.EndModal(wx.ID_OK)
		renamed = yield from self._waitFor(lambda: "Smoke renamed" in [listCtrl.GetItemText(i) for i in range(listCtrl.GetItemCount())], 5000)
		report(bool(renamed) and dialogs.state.current.title == "Smoke renamed" and dialog.GetTitle() == "Ask AI - Smoke renamed",
			"rename updates the list, the conversation and the Ask dialog title", repr(dialog.GetTitle()))

		# Archive and unarchive the first conversation.
		index = [listCtrl.GetItemText(i) for i in range(listCtrl.GetItemCount())].index(QUESTION)
		listCtrl.Select(index)
		listCtrl.Focus(index)
		yield 300
		count = listCtrl.GetItemCount()
		spoken.clear()
		press(manager.archiveButton)
		yield from self._waitFor(lambda: "Archived" in spoken, 3000)
		report(listCtrl.GetItemCount() == count - 1 and store.load(firstId).archived, "Archive hides the conversation", repr(spoken))
		report(listCtrl.GetFirstSelected() >= 0 and wx.Window.FindFocus() is listCtrl, "focus stays on the list after archiving")
		manager.archivedCheck.SetValue(True)
		event = wx.CommandEvent(wx.wxEVT_CHECKBOX, manager.archivedCheck.GetId())
		event.SetEventObject(manager.archivedCheck)
		manager.archivedCheck.GetEventHandler().ProcessEvent(event)
		titles = [listCtrl.GetItemText(i) for i in range(listCtrl.GetItemCount())]
		archivedTitle = QUESTION + " (archived)"
		report(archivedTitle in titles, "Show archived lists it again", repr(titles))
		listCtrl.Select(titles.index(archivedTitle))
		yield 200
		report(manager.archiveButton.GetLabel() == "Un&archive", "the Archive button flips to Unarchive")
		press(manager.archiveButton)
		yield from self._waitFor(lambda: not store.load(firstId).archived, 3000)
		report(not store.load(firstId).archived, "Unarchive brings it back")

		# Export the first conversation as text into Documents\Ask AI.
		titles = [listCtrl.GetItemText(i) for i in range(listCtrl.GetItemCount())]
		listCtrl.Select(titles.index(QUESTION))
		yield 200
		spoken.clear()
		press(manager.exportButton)
		yield from self._saveAs(manager, 2, "Smoke export", "Export conversation")
		exported = os.path.join(documents, "Smoke export.txt")
		yield from self._waitFor(lambda: os.path.exists(exported), 5000)
		try:
			with open(exported, encoding="utf-8-sig") as f:
				text = f.read()
			report("You" in text and QUESTION in text and ANSWER_SPOKEN in text, "conversation exported as text", text[:200])
		except OSError as e:
			report(False, "conversation exported as text", repr(e))

		# Import a copy of a conversation file (as if copied from another PC), then delete it.
		import json
		import uuid

		copyPath = os.path.join(FAKE_DIR, "imported.json")
		data = store.load(firstId).toDict()
		data["id"] = uuid.uuid4().hex
		data["title"] = "Imported smoke"
		with open(copyPath, "w", encoding="utf-8") as f:
			json.dump(data, f)
		FakeFileDialog.path = copyPath
		original = wx.FileDialog
		wx.FileDialog = FakeFileDialog
		count = listCtrl.GetItemCount()
		spoken.clear()
		try:
			press(findButton(manager, "&Import..."))
			yield from self._waitFor(lambda: "Imported Imported smoke" in spoken, 5000)
		finally:
			wx.FileDialog = original
		report(listCtrl.GetItemCount() == count + 1 and store.load(data["id"]) is not None, "Import adds the conversation", repr(spoken))
		report(manager._items[listCtrl.GetFirstSelected()].id == data["id"], "the imported conversation is selected")
		spoken.clear()
		press(manager.deleteButton)
		confirm = yield from self._waitFor(findMessageDialog, 5000)
		if report(confirm is not None, "Delete asks for confirmation"):
			texts = [w.GetLabel() for w in walk(confirm) if isinstance(w, wx.StaticText)]
			buttons = [w.GetLabel() for w in walk(confirm) if isinstance(w, wx.Button)]
			report(buttons == ["&Yes", "&No"], "delete confirmation has Yes and No only", repr((texts, buttons)))
			press(next(w for w in walk(confirm) if isinstance(w, wx.Button) and w.GetId() == wx.ID_YES))
			yield from self._waitFor(lambda: findMessageDialog() is None, 5000)
		yield from self._waitFor(lambda: "Deleted" in spoken, 3000)
		report(store.load(data["id"]) is None and listCtrl.GetItemCount() == count, "Delete removes the conversation file")
		report(listCtrl.GetFirstSelected() >= 0, "an item stays selected after deleting")

		# Continue the first conversation.
		titles = [listCtrl.GetItemText(i) for i in range(listCtrl.GetItemCount())]
		listCtrl.Select(titles.index(QUESTION))
		yield 200
		spoken.clear()
		press(manager.continueButton)
		yield from self._waitFor(lambda: findTopLevelOfType("ConversationManagerDialog") is None, 5000)
		yield from self._waitFor(lambda: any(str(s).startswith("Continuing") for s in spoken), 3000)
		report(dialogs.state.current.id == firstId and dialog.GetTitle() == "Ask AI - " + QUESTION
			and ("ChatGPT: " + ANSWER_SPOKEN) in dialog.conversationCtrl.GetValue(),
			"Continue loads the conversation into the Ask dialog", repr(spoken))
		ctrl = dialog.conversationCtrl
		after = ctrl.GetRange(ctrl.GetInsertionPoint(), ctrl.GetLastPosition())
		report(after.startswith("ChatGPT: "), "caret on the newest answer after continuing", repr(after[:60]))
		report(wx.Window.FindFocus() is dialog.questionCtrl, "focus in the Question box after continuing")

		# The Tools menu item opens the manager when the Ask dialog is closed.
		press(dialog.FindWindowById(wx.ID_CLOSE))
		yield from self._waitFor(lambda: dialogs.AskAIDialog.getInstance() is None, 3000)
		plugin = next(p for p in globalPluginHandler.runningPlugins if type(p).__module__ == "globalPlugins.askAI")
		event = wx.CommandEvent(wx.wxEVT_MENU, plugin.conversationsMenuItem.GetId())
		gui.mainFrame.sysTrayIcon.ProcessEvent(event)
		manager = yield from self._waitFor(lambda: findTopLevelOfType("ConversationManagerDialog"), 5000)
		report(manager is not None, "Tools menu Ask AI conversations opens the manager")
		if manager is not None:
			press(manager.FindWindowById(wx.ID_CLOSE))
			yield from self._waitFor(lambda: findTopLevelOfType("ConversationManagerDialog") is None, 3000)
		yield 300

	def _claude(self, dialogs, store):
		"""Ask AI 1.2: switch a conversation to Claude, get an answer from the fake Claude Code, project files,
		a not-signed-in error, then switch back to ChatGPT."""
		providersModule = sys.modules["globalPlugins.askAI.providers"]
		yield from self._gesture(keyboardHandler.KeyboardInputGesture.fromName("NVDA+alt+g"))
		dialog = yield from self._waitFor(dialogs.AskAIDialog.getInstance, 5000)
		if not report(dialog is not None, "Ask dialog opened for the Claude steps"):
			return
		yield 800
		before = dialogs.state.current.id
		spoken.clear()
		press(findButton(dialog, "&New conversation"))
		# press() only posts the click: wait until it has been handled.
		yield from self._waitFor(lambda: dialogs.state.current.id != before and "New conversation started." in spoken, 3000)
		yield 300
		services = choiceItems(dialog.serviceChoice)
		report(services == ["ChatGPT (Codex)", "Claude (Claude Code)"] and dialog.serviceChoice.GetSelection() == 0,
			"AI service choice lists ChatGPT and Claude, ChatGPT selected", repr(services))
		spoken.clear()
		choose(dialog.serviceChoice, 1)
		yield from self._waitFor(lambda: "Now using Claude" in spoken, 3000)
		report("Now using Claude" in spoken, "switching the service announces Now using Claude", repr(spoken))
		models = choiceItems(dialog.modelChoice)
		efforts = choiceItems(dialog.effortChoice)
		report(models == ["Default (your plan's default)", "Sonnet", "Opus", "Haiku", "Fable"],
			"model list follows the service (Claude Code aliases)", repr(models))
		report(efforts == ["Low (fastest)", "Medium", "High", "Extra high", "Maximum"], "effort list follows the service", repr(efforts))
		choose(dialog.modelChoice, models.index("Opus"))
		yield from self._waitFor(lambda: "Model changed to Opus" in spoken, 3000)
		current = dialogs.state.current
		report((current.provider, current.model) == ("claude", "opus"), "conversation stores the service and model",
			repr((current.provider, current.model)))
		spoken.clear()
		yield from self._ask(dialog, dialogs, "Bonjour? Ask Claude")
		phase("claude-answer")
		calls = readFake("calls.log", FAKE_CLAUDE_DIR)
		lastCall = calls[calls.rfind('ARGS: "-p"'):]
		info("fake claude calls.log tail: %r" % lastCall[:700])
		report('"--output-format" "json"' in lastCall and '"--tools" "" ' in lastCall and '"--permission-mode" "dontAsk"' in lastCall
			and '"--restricted"' in lastCall and '"--model" "opus"' in lastCall and "dangerously" not in lastCall
			and "bypass" not in lastCall.lower() and "Bonjour" not in lastCall,
			"Claude runs with -p, JSON output, no tools, the chosen model and the question only on stdin", repr(lastCall[:600]))
		stdin = readFake("last-stdin.txt", FAKE_CLAUDE_DIR)
		report("Question: Bonjour? Ask Claude" in stdin, "Claude prompt arrived on stdin", repr(stdin[:300]))
		report("Asking Claude\u2026" in spoken and CLAUDE_ANSWER_SPOKEN in spoken, "Asking Claude announced and the answer spoken",
			repr(spoken))
		report(("Claude: " + CLAUDE_ANSWER_SPOKEN) in dialog.conversationCtrl.GetValue(), "answer labelled Claude in the Conversation box",
			repr(dialog.conversationCtrl.GetValue()[-200:]))
		saved = store.load(current.id)
		report(saved is not None and saved.provider == "claude" and saved.messages[-1].get("provider") == "claude",
			"saved conversation records Claude for the conversation and the answer", repr(saved.toDict() if saved else None))
		claudeId = current.id

		# Claude in a project folder: only file tools, edits accepted in the folder, file announced.
		original = wx.DirDialog
		wx.DirDialog = FakeDirDialog
		try:
			press(dialog.chooseProjectButton)
			yield from self._waitFor(lambda: dialogs.state.current.projectFolder == PROJECT, 3000)
		finally:
			wx.DirDialog = original
		yield 300
		spoken.clear()
		yield from self._ask(dialog, dialogs, "CLAUDEFILE please")
		calls = readFake("calls.log", FAKE_CLAUDE_DIR)
		lastCall = calls[calls.rfind('ARGS: "-p"'):]
		report('"--tools" "Read,Write,Edit,Glob,Grep"' in lastCall and '"--permission-mode" "acceptEdits"' in lastCall
			and ("CWD: " + PROJECT).lower() in lastCall.lower(), "Claude in a project folder: file tools only, cwd is the folder",
			repr(lastCall[:600]))
		stdin = readFake("last-stdin.txt", FAKE_CLAUDE_DIR)
		report("cannot run commands" in stdin, "Claude's project folder instructions are in the prompt")
		report("Created: claude-report.md" in spoken, "file written by Claude is announced", repr(spoken))
		press(dialog.clearProjectButton)
		yield from self._waitFor(lambda: not dialogs.state.current.projectFolder, 3000)
		dialog.questionCtrl.SetFocus()
		yield 300

		# Claude not signed in: named in the error and offered the settings (answer No).
		dialog.questionCtrl.SetValue("CLAUDE401 question")
		dialog.questionCtrl.SetInsertionPointEnd()
		self.sim.Char(wx.WXK_RETURN)
		offer = yield from self._waitFor(findMessageDialog, 15000)
		if report(offer is not None, "Claude sign-in error shows a message dialog"):
			yield 500
			texts = [w.GetLabel() for w in walk(offer) if isinstance(w, wx.StaticText)]
			report(any("Claude Code is not signed in to Claude" in t for t in texts), "the error names Claude Code and Claude", repr(texts))
			press(next((w for w in walk(offer) if isinstance(w, wx.Button) and w.GetId() == wx.ID_NO), offer))
			yield from self._waitFor(lambda: findMessageDialog() is None, 5000)
		yield 500
		report("Sign in to Claude in NVDA Settings > Ask AI" in dialog.conversationCtrl.GetValue(),
			"Conversation box tells where to sign in to Claude", repr(dialog.conversationCtrl.GetValue()[-300:]))
		dialog.questionCtrl.SetValue("")

		# Back to ChatGPT: the model list follows.
		spoken.clear()
		choose(dialog.serviceChoice, 0)
		yield from self._waitFor(lambda: "Now using ChatGPT" in spoken, 3000)
		report("Now using ChatGPT" in spoken and "GPT Test Fast" in choiceItems(dialog.modelChoice)
			and dialogs.state.current.provider == "codex", "switching back to ChatGPT restores Codex's models", repr(spoken))
		choose(dialog.serviceChoice, 1)
		yield 300

		# The conversation manager shows the service.
		press(findButton(dialog, "Conve&rsations..."))
		manager = yield from self._waitFor(lambda: findTopLevelOfType("ConversationManagerDialog"), 5000)
		if report(manager is not None, "conversation manager opened for the Service column"):
			yield 800
			listCtrl = manager.listCtrl
			rows = [(listCtrl.GetItemText(i), listCtrl.GetItemText(i, 4)) for i in range(listCtrl.GetItemCount())]
			info("conversation rows: %r" % rows)
			index = next((i for i, c in enumerate(manager._items) if c.id == claudeId), -1)
			report(index >= 0 and listCtrl.GetItemText(index, 4) == "Claude" and all(r[1] in ("ChatGPT", "Claude") for r in rows),
				"Service column shows each conversation's service (Claude here)", repr(rows))
			phase("manager-service-column")
			yield 500
			# FindWindow searches the manager's own children (FindWindowById is static and could find the Ask dialog's Close).
			press(manager.FindWindow(wx.ID_CLOSE))
			yield from self._waitFor(lambda: findTopLevelOfType("ConversationManagerDialog") is None, 3000)
		# The Claude conversation stays saved; a new (default service, ChatGPT) conversation is current for later steps.
		press(findButton(dialog, "&New conversation"))
		yield from self._waitFor(lambda: dialogs.state.current.id != claudeId, 3000)
		report(dialogs.state.current.provider == "codex", "a new conversation uses the default service (ChatGPT)")
		press(dialog.FindWindowById(wx.ID_CLOSE))
		yield from self._waitFor(lambda: dialogs.AskAIDialog.getInstance() is None, 3000)
		report(providersModule.isKnown("claude"), "providers registry knows Claude")
		yield 300

	def _combo(self, key, modifiers, effect, fallback, name):
		"""Press a key combination with SendInput; if it has no effect, run fallback via wx.CallAfter."""
		self.sim.Char(key, modifiers)
		ok = yield from self._waitFor(effect, 2500)
		if ok:
			info("%s delivered by SendInput" % name)
			return "SendInput"
		info("%s not seen after SendInput; calling the handler" % name)
		wx.CallAfter(fallback)
		yield from self._waitFor(effect, 3000)
		return "handler"

	def _find(self, dialog, query):
		"""Ctrl+F, type query into the real find prompt, OK."""
		yield from self._combo(ord("F"), wx.MOD_CONTROL, lambda: findTopLevelOfType("TextEntryDialog", "Find"), dialog.onFind, "Ctrl+F")
		prompt = yield from self._waitFor(lambda: findTopLevelOfType("TextEntryDialog", "Find"), 3000)
		if prompt is None:
			return None
		prefilled = prompt.GetValue()
		prompt.SetValue(query)
		prompt.EndModal(wx.ID_OK)
		return prefilled

	def _round6(self, dialogs, store, part="all"):
		"""Search, question recall, drafts, status ("a"); "Ask about this", attachments, long pastes and the
		manager's Search field ("b"); or both ("all")."""
		import subprocess

		phase("round6")
		yield from self._gesture(keyboardHandler.KeyboardInputGesture.fromName("NVDA+alt+g"))
		dialog = yield from self._waitFor(dialogs.AskAIDialog.getInstance, 5000)
		if not report(dialog is not None, "Ask dialog opened for the round 6 steps"):
			return
		yield 800
		conversation = dialogs.state.current
		yield from self._ask(dialog, dialogs, "What is the capital of France?")
		yield from self._ask(dialog, dialogs, "Tell me about the capital again")
		title = conversation.title
		if part in ("all", "a"):
			yield from self._round6a(dialogs, store, dialog, conversation, title)
		if part in ("all", "b"):
			yield from self._round6b(dialogs, store, title)
		if part in ("all", "c"):
			yield from self._round6c(dialogs, store, title)

	def _round6a(self, dialogs, store, dialog, conversation, title):
		# 1. Find in the Conversation box (two answers mention Paris).
		spoken.clear()
		yield from self._find(dialog, "PARIS")
		yield from self._waitFor(lambda: any(str(x).startswith("Match ") for x in spoken), 3000)
		ctrl = dialog.conversationCtrl
		selected = ctrl.GetStringSelection()
		report(any(str(x).startswith("Match 1 of 2. ChatGPT: Paris is the capital of France.") for x in spoken),
			"Ctrl+F finds the first match and says Match 1 of 2 with the line", repr(spoken))
		report(wx.Window.FindFocus() is ctrl and selected.lower() == "paris", "the match is selected in the focused Conversation box",
			repr(selected))
		spoken.clear()
		yield from self._combo(wx.WXK_F3, wx.MOD_NONE, lambda: any("Match 2 of 2" in str(x) for x in spoken),
			lambda: dialog.findNext(True), "F3")
		report(any(str(x).startswith("Match 2 of 2.") for x in spoken), "F3 goes to the next match", repr(spoken))
		spoken.clear()
		yield from self._combo(wx.WXK_F3, wx.MOD_NONE, lambda: any("Wrapped" in str(x) for x in spoken),
			lambda: dialog.findNext(True), "F3 (wrap)")
		report(any(str(x).startswith("Wrapped to first match. Match 1 of 2.") for x in spoken), "F3 wraps to the first match", repr(spoken))
		spoken.clear()
		yield from self._combo(wx.WXK_F3, wx.MOD_SHIFT, lambda: any("Wrapped" in str(x) for x in spoken),
			lambda: dialog.findNext(False), "Shift+F3")
		report(any(str(x).startswith("Wrapped to last match. Match 2 of 2.") for x in spoken), "Shift+F3 wraps to the last match", repr(spoken))
		spoken.clear()
		prefilled = yield from self._find(dialog, "zebra")
		yield from self._waitFor(lambda: "No matches for zebra" in spoken, 3000)
		report(prefilled == "PARIS" and "No matches for zebra" in spoken, "Ctrl+F is prefilled with the last search; no match is announced",
			repr((prefilled, spoken)))

		# 2. Question recall with Up and Down, and drafts.
		dialog.questionCtrl.SetValue("")
		dialog.questionCtrl.SetFocus()
		yield 300
		spoken.clear()
		yield from self._combo(wx.WXK_UP, wx.MOD_NONE, lambda: dialog.questionCtrl.GetValue(), lambda: dialog.recallQuestion(True), "Up")
		report(dialog.questionCtrl.GetValue() == "Tell me about the capital again" and "Tell me about the capital again" in spoken,
			"Up recalls and reads the newest question", repr(dialog.questionCtrl.GetValue()))
		yield from self._combo(wx.WXK_UP, wx.MOD_NONE, lambda: dialog.questionCtrl.GetValue() == "What is the capital of France?",
			lambda: dialog.recallQuestion(True), "Up again")
		report(dialog.questionCtrl.GetValue() == "What is the capital of France?", "Up again recalls the earlier question")
		spoken.clear()
		yield from self._combo(wx.WXK_DOWN, wx.MOD_NONE, lambda: dialog.questionCtrl.GetValue() == "Tell me about the capital again",
			lambda: dialog.recallQuestion(False), "Down")
		yield from self._combo(wx.WXK_DOWN, wx.MOD_NONE, lambda: "No later question" in spoken, lambda: dialog.recallQuestion(False), "Down again")
		report("No later question" in spoken and dialog.questionCtrl.GetValue() == "", "Down past the newest says No later question and restores the box",
			repr(spoken))
		dialog.questionCtrl.SetValue("half-written draft")
		dialog.questionCtrl.SetInsertionPointEnd()
		dialog.questionCtrl.SetFocus()
		yield 300
		spoken.clear()
		yield from self._combo(ord("S"), wx.MOD_CONTROL, lambda: "Draft saved" in spoken, lambda: dialog.saveDraft(), "Ctrl+S")
		report("Draft saved" in spoken and store.load(conversation.id).draft == "half-written draft", "Ctrl+S saves the draft in the JSON",
			repr(spoken))
		press(dialog.FindWindowById(wx.ID_CLOSE))
		yield from self._waitFor(lambda: dialogs.AskAIDialog.getInstance() is None, 3000)
		spoken.clear()
		yield from self._gesture(keyboardHandler.KeyboardInputGesture.fromName("NVDA+alt+g"))
		dialog = yield from self._waitFor(dialogs.AskAIDialog.getInstance, 5000)
		yield from self._waitFor(lambda: "Draft restored" in spoken, 3000)
		report(dialog is not None and dialog.questionCtrl.GetValue() == "half-written draft" and "Draft restored" in spoken,
			"reopening restores the draft and says Draft restored", repr(spoken))
		if dialog is None:
			return
		yield 500

		# 3. Status report, and "Done:" when the answer arrives with the dialog closed.
		dialog.questionCtrl.SetValue("MEDIUM status question")
		dialog.questionCtrl.SetFocus()
		self.sim.Char(wx.WXK_RETURN)
		yield from self._waitFor(lambda: dialogs.state.pending is not None, 3000)
		report(store.load(conversation.id).draft == "", "sending clears the draft")
		statusGesture = keyboardHandler.KeyboardInputGesture.fromName("NVDA+alt+shift+s")
		report(getattr(scriptHandler.findScript(statusGesture), "__name__", "") == "script_reportStatus",
			"NVDA+Alt+Shift+S resolves to script_reportStatus")
		spoken.clear()
		yield from self._gesture(statusGesture)
		yield from self._waitFor(lambda: any("is answering in" in str(x) for x in spoken), 3000)
		report(any(str(x).startswith("ChatGPT is answering in %s, " % title) for x in spoken), "status while answering", repr(spoken))
		spoken.clear()
		press(dialog.FindWindowById(wx.ID_CLOSE))
		yield from self._waitFor(lambda: any(str(x).startswith("Done: ") for x in spoken), 15000)
		report(any(str(x) == "Done: %s. Paris is the capital of France." % title for x in spoken),
			"an answer arriving with the dialog closed starts with Done: <title>.", repr(spoken))
		spoken.clear()
		yield from self._gesture(statusGesture)
		yield from self._waitFor(lambda: spoken, 3000)
		report(any(str(x).startswith("Idle. Last answer from ChatGPT at ") and str(x).endswith(" in %s." % title) for x in spoken),
			"status when idle names the last answer", repr(spoken))

	def _round6b(self, dialogs, store, title):
		dialog = dialogs.AskAIDialog.getInstance()
		if dialog is not None:
			press(dialog.FindWindowById(wx.ID_CLOSE))
			yield from self._waitFor(lambda: dialogs.AskAIDialog.getInstance() is None, 3000)

		# 4. "Ask about this" in Notepad, with a screenshot.
		aboutGesture = keyboardHandler.KeyboardInputGesture.fromName("NVDA+alt+a")
		report(getattr(scriptHandler.findScript(aboutGesture), "__name__", "") == "script_askAboutThis",
			"NVDA+Alt+A resolves to script_askAboutThis")
		# Wine without a window manager does not bring another process (Notepad) to the foreground, so the
		# "other app" is an editor window in NVDA's own process: a standard multi-line edit control.
		editorFrame = wx.Frame(None, title="Report.txt - Smoke editor", size=(500, 300))
		editor = wx.TextCtrl(editorFrame, style=wx.TE_MULTILINE)
		with open(FILES + "\\context.txt", encoding="utf-8") as f:
			editor.SetValue(f.read())
		editorFrame.Show()
		editorFrame.Raise()
		editor.SetFocus()
		editor.SetSelection(-1, -1)
		try:
			ready = yield from self._waitFor(lambda: api.getFocusObject().windowHandle == editor.GetHandle(), 8000)
			report(bool(ready), "the editor window has the NVDA focus", repr(api.getForegroundObject().name))
			yield 800
			spoken.clear()
			yield from self._gesture(aboutGesture)
			dialog = yield from self._waitFor(dialogs.AskAIDialog.getInstance, 8000)
			yield from self._waitFor(lambda: any("Context attached from" in str(x) for x in spoken), 5000)
		except BaseException:
			editorFrame.Destroy()
			raise
		if not report(dialog is not None, "NVDA+Alt+A opens the Ask dialog", repr(spoken)):
			return
		yield 500
		phase("ask-about-this")
		summary = dialog.contextCtrl.GetValue()
		report(dialogs.state.pendingContext is not None and "Quarterly figures" in dialogs.state.pendingContext.text,
			"the editor's selected text is attached", repr(dialogs.state.pendingContext.text[:80] if dialogs.state.pendingContext else None))
		report(" characters from " in summary and dialog.contextCtrl.IsShown() and dialog.contextCtrl.AcceptsFocusFromKeyboard(),
			"Context field shows the summary and is Tab-reachable", repr(summary))
		report(any(str(x).startswith("Context attached from ") and str(x).endswith("Type your question.") for x in spoken),
			"attaching context is announced", repr(spoken))
		report(wx.Window.FindFocus() is dialog.questionCtrl, "focus is in the Question box after NVDA+Alt+A")
		hasShot = dialog.screenshotCheck.IsEnabled()
		info("screenshot captured under Wine: %s" % hasShot)
		if hasShot:
			dialog.screenshotCheck.SetValue(True)
			event = wx.CommandEvent(wx.wxEVT_CHECKBOX, dialog.screenshotCheck.GetId())
			event.SetEventObject(dialog.screenshotCheck)
			dialog.screenshotCheck.GetEventHandler().ProcessEvent(event)
		yield from self._ask(dialog, dialogs, "Summarize this")
		editorFrame.Destroy()
		calls = readFake("calls.log")
		lastCall = calls[calls.rfind('ARGS: "exec"'):]
		stdin = readFake("last-stdin.txt")
		report("Text from the user's screen" in stdin and "Quarterly figures: revenue 42 million." in stdin,
			"the screen text is fenced in the prompt (stdin)", repr(stdin[:300]))
		if hasShot:
			report('"--image=' in lastCall and "screenshot.png" in lastCall and "Quarterly" not in lastCall,
				"Codex gets the screenshot with --image=, never the text on argv", repr(lastCall[:500]))
		saved = store.load(dialogs.state.current.id)
		question = [m for m in saved.messages if m.get("role") == "user"][-1]
		report(" characters from " in question.get("context", "") and "Quarterly" not in str(saved.toDict()),
			"the stored question keeps the summary, not the screen text", repr(question))
		requests = os.path.join(dialogs.workDir, "requests")
		report(not os.path.isdir(requests) or not os.listdir(requests), "the request folder was deleted",
			repr(os.listdir(requests) if os.path.isdir(requests) else None))
		report(not dialog.contextCtrl.IsShown(), "the context applies to one question only")

		# A password field is refused.
		frame = wx.Frame(None, title="Smoke password")
		password = wx.TextCtrl(frame, style=wx.TE_PASSWORD)
		frame.Show()
		password.SetFocus()
		yield from self._waitFor(lambda: api.getFocusObject().windowHandle == password.GetHandle(), 5000)
		yield 500
		spoken.clear()
		yield from self._gesture(aboutGesture)
		yield from self._waitFor(lambda: spoken, 3000)
		report("Can't use protected or password fields" in spoken, "NVDA+Alt+A refuses a password field",
			repr((spoken, api.getFocusObject().role)))
		frame.Destroy()
		yield 500

	def _round6c(self, dialogs, store, title):
		dialog = dialogs.AskAIDialog.getInstance()
		if dialog is None:
			yield from self._gesture(keyboardHandler.KeyboardInputGesture.fromName("NVDA+alt+g"))
			dialog = yield from self._waitFor(dialogs.AskAIDialog.getInstance, 5000)
		dialog.Raise()
		yield 500

		# 5. Attach files (Codex: picture as --image, text inlined, other file copied).
		FakeFileDialog.paths = [FILES + "\\notes.txt", FILES + "\\pic.png", FILES + "\\data.bin"]
		original = wx.FileDialog
		wx.FileDialog = FakeFileDialog
		spoken.clear()
		try:
			press(dialog.attachButton)
			yield from self._waitFor(lambda: "3 files attached" in spoken, 5000)
		finally:
			wx.FileDialog = original
		items = [dialog.attachmentsList.GetString(i) for i in range(dialog.attachmentsList.GetCount())]
		report("3 files attached" in spoken and len(items) == 3 and wx.Window.FindFocus() is dialog.attachmentsList,
			"Attach files lists the files and announces them", repr((spoken, items)))
		phase("attachments")
		yield 300
		yield from self._ask(dialog, dialogs, "Use the attached files")
		calls = readFake("calls.log")
		lastCall = calls[calls.rfind('ARGS: "exec"'):]
		stdin = readFake("last-stdin.txt")
		report('"--image=' in lastCall and "pic.png" in lastCall, "the picture goes to Codex with --image=", repr(lastCall[:400]))
		report("Attached file notes.txt:" in stdin and "Buy milk and bread." in stdin and "data.bin" in stdin,
			"text file inlined, other file named in the prompt", repr(stdin[:400]))
		saved = store.load(dialogs.state.current.id)
		question = [m for m in saved.messages if m.get("role") == "user"][-1]
		report(question.get("attachments") == ["notes.txt", "pic.png", "data.bin"], "attachment names stored with the question", repr(question))

		# Claude: the picture is opened with Read from the request folder (its working directory).
		choose(dialog.serviceChoice, 1)
		yield 300
		FakeFileDialog.paths = [FILES + "\\pic.png"]
		wx.FileDialog = FakeFileDialog
		try:
			press(dialog.attachButton)
			yield from self._waitFor(lambda: dialog.attachmentsList.GetCount() == 1, 5000)
		finally:
			wx.FileDialog = original
		yield from self._ask(dialog, dialogs, "Describe the picture")
		calls = readFake("calls.log", FAKE_CLAUDE_DIR)
		lastCall = calls[calls.rfind('ARGS: "-p"'):]
		report('"--tools" "Read" ' in lastCall and "\\requests\\request-" in lastCall.split("CWD:")[-1],
			"Claude gets only Read and runs in the request folder", repr(lastCall[:600]))
		choose(dialog.serviceChoice, 0)
		yield 300

		# Long paste becomes question.txt.
		spoken.clear()
		yield from self._ask(dialog, dialogs, "long " * 7000)
		stdin = readFake("last-stdin.txt")
		report("Long text attached as a file" in spoken and "Attached file question.txt:" in stdin, "a long paste is sent as question.txt",
			repr(spoken))

		# 6. Search all conversations in the manager.
		press(findButton(dialog, "Conve&rsations..."))
		manager = yield from self._waitFor(lambda: findTopLevelOfType("ConversationManagerDialog"), 5000)
		if report(manager is not None, "conversation manager opened for Search"):
			yield 800
			mnemonicsFound = mnemonics(manager)
			report(not {k: v for k, v in mnemonicsFound.items() if len(v) > 1} and "s" in mnemonicsFound,
				"manager mnemonics unique, Search has S", repr(mnemonicsFound))
			spoken.clear()
			manager.searchCtrl.SetFocus()
			manager.searchCtrl.SetValue("WHAT IS THE CAPITAL")
			yield from self._waitFor(lambda: any("match" in str(x) for x in spoken), 3000)
			rows = [manager.listCtrl.GetItemText(i) for i in range(manager.listCtrl.GetItemCount())]
			report(any(str(x).endswith(("conversation matches", "conversations match")) for x in spoken) and title in rows,
				"Search filters by title (case-insensitive) and announces the count", repr((spoken, rows)))
			spoken.clear()
			manager.searchCtrl.SetValue("CAPITAL AGAIN")
			yield from self._waitFor(lambda: any("match" in str(x) for x in spoken), 3000)
			rows = [manager.listCtrl.GetItemText(i) for i in range(manager.listCtrl.GetItemCount())]
			report("1 conversation matches" in spoken and rows == [title], "Search finds message text", repr((spoken, rows)))
			spoken.clear()
			manager.searchCtrl.SetValue("revenue 42")
			yield from self._waitFor(lambda: any("match" in str(x) for x in spoken), 3000)
			report("0 conversations match" in spoken, "screen text is not stored, so it is not found", repr(spoken))
			spoken.clear()
			manager.searchCtrl.SetValue("zzzz nothing")
			yield from self._waitFor(lambda: any("match" in str(x) for x in spoken), 3000)
			report("0 conversations match" in spoken and manager.listCtrl.GetItemCount() == 0 and manager.emptyText.IsShown()
				and wx.Window.FindFocus() is manager.searchCtrl, "no match: empty list, message shown, focus stays in Search", repr(spoken))
			phase("manager-search")
			yield 300
			press(manager.FindWindow(wx.ID_CLOSE))
			yield from self._waitFor(lambda: findTopLevelOfType("ConversationManagerDialog") is None, 3000)
		press(dialog.FindWindowById(wx.ID_CLOSE))
		yield from self._waitFor(lambda: dialogs.AskAIDialog.getInstance() is None, 3000)
		yield 300

	def _run(self):
		info("NVDA %s, Python %s, %d-bit, configPath=%s" % (
			buildVersion.version, sys.version.split()[0], 64 if sys.maxsize > 2 ** 32 else 32,
			globalVars.appArgs.configPath,
		))
		installSpies()

		# 1. The add-on loaded and registered itself.
		plugin = next((p for p in globalPluginHandler.runningPlugins if type(p).__module__ == "globalPlugins.askAI"), None)
		if not report(plugin is not None, "askAI GlobalPlugin is running"):
			self._exit()
			return
		dialogs = sys.modules["globalPlugins.askAI.dialogs"]
		codexClient = sys.modules["globalPlugins.askAI.providers.base"]
		conf = config.conf["askAI"]
		report(conf["codexPath"] == FAKE_CODEX, "config spec registered and codexPath is the fake", repr(conf["codexPath"]))
		report(dialogs.AskAISettingsPanel in NVDASettingsDialog.categoryClasses, "settings panel registered")
		item = plugin.toolsMenuItem
		report(item is not None and item.GetItemLabel() == "As&k AI...", "Tools menu item added",
			repr(item.GetItemLabel() if item else None))
		item = getattr(plugin, "conversationsMenuItem", None)
		report(item is not None and item.GetItemLabel() == "Ask AI con&versations...", "Tools menu has Ask AI conversations",
			repr(item.GetItemLabel() if item else None))
		report(hasattr(plugin, "script_showConversations"), "Show Ask AI conversations script exists (no default gesture)")
		report(bool(dialogs.workDir), "workDir set", repr(dialogs.workDir))
		report(dialogs.workDir.lower().endswith("appdata\\local\\askai\\workdir"), "workDir is %LOCALAPPDATA%\\askAI\\workdir",
			repr(dialogs.workDir))

		# 1b. Migration from Ask GPT 1.1: the [askGPT] settings (Ask AI had none) and the saved conversation.
		report(conf["migratedFromAskGPT"] and conf["dictationSilenceTimeout"] == 3,
			"Ask GPT settings copied to Ask AI (codexPath, dictation timeout)", repr((conf["codexPath"], conf["dictationSilenceTimeout"])))
		migrated = dialogs.state.store.load(MIGRATED_ID)
		report(migrated is not None and migrated.provider == "codex" and migrated.archived
			and migrated.messages[-1].get("provider") == "codex", "Ask GPT conversation copied and read as a ChatGPT conversation",
			repr(migrated.toDict() if migrated else None))
		oldFile = os.path.join(os.environ.get("LOCALAPPDATA", ""), "askGPT", "conversations", MIGRATED_ID + ".json")
		report(os.path.isfile(oldFile), "the Ask GPT conversation file was copied, not moved", oldFile)
		# The fake Claude Code (Ask AI's own setting; Ask GPT had none).
		conf["claudePath"] = FAKE_CLAUDE

		# 1c. The old Ask GPT add-on is still installed: Ask AI asks once whether to remove it.
		import addonHandler

		old = next((a for a in addonHandler.getAvailableAddons() if a.name == "askGPT"), None)
		report(old is not None and old.isEnabled and not old.isPendingRemove, "stand-in Ask GPT add-on is installed and enabled",
			repr(old))
		prompt = yield from self._waitFor(findMessageDialog, 20000)
		if report(prompt is not None, "Ask AI asks about removing the old Ask GPT add-on"):
			yield 500
			phase("old-addon-prompt")
			yield 500
			texts = [w.GetLabel() for w in walk(prompt) if isinstance(w, wx.StaticText)]
			buttons = [w.GetLabel() for w in walk(prompt) if isinstance(w, wx.Button)]
			report(any("The old Ask GPT add-on is still installed. Remove it now?" in t for t in texts) and buttons == ["&Yes", "&No"],
				"old add-on question has the expected text and Yes/No only", repr((texts, buttons)))
			noButton = next((w for w in walk(prompt) if isinstance(w, wx.Button) and w.GetId() == wx.ID_NO), None)
			press(noButton)
			yield from self._waitFor(lambda: findMessageDialog() is None, 5000)
		yield 300
		report(conf["askedToRemoveAskGPT"] and old is not None and not old.isPendingRemove,
			"answering No keeps Ask GPT and is remembered (never asked again)")
		conversations = sys.modules["globalPlugins.askAI.conversations"]
		store = dialogs.state.store
		report(store.folder.lower().endswith("appdata\\local\\askai\\conversations"), "conversations folder is %LOCALAPPDATA%\\askAI\\conversations",
			repr(store.folder))
		models = dialogs.state.reloadModels()
		report([m.slug for m in models] == ["gpt-test-fast", "gpt-test-deep"], "model list read from %USERPROFILE%\\.codex\\models_cache.json",
			repr(models))
		command = codexClient.buildCommand(FAKE_CODEX, ["login", "status"])
		report(isinstance(command, str) and "cmd.exe\" /d /v:off /s /c" in command,
			"fake codex.cmd is not an npm shim, so the cmd.exe fallback is used", repr(command))

		# ASKAI_SMOKE_ONLY=new: skip the Ask GPT 1.1 steps (2 to 8) and go straight to the Claude steps, then the
		# settings and the exit checks. Fewer child processes before the new features are reached, because Wine 10's
		# wineserver can crash (queue.c add_queue_hook_count assertion) after many short-lived hooked processes.
		only = os.environ.get("ASKAI_SMOKE_ONLY", "").lower()
		focused = only in ("new", "round6", "round6a", "round6b", "round6c")
		cancelledAt = time.monotonic() - 60
		if focused:
			info("focused run: %s" % only)
			if only == "new":
				yield from self._claude(dialogs, store)
			yield from self._round6(dialogs, store, {"round6a": "a", "round6b": "b", "round6c": "c"}.get(only, "all"))
		else:
			# 2. Open the Ask dialog through the real gesture binding.
			gesture = keyboardHandler.KeyboardInputGesture.fromName("NVDA+alt+g")
			script = scriptHandler.findScript(gesture)
			report(getattr(script, "__name__", "") == "script_askAI", "NVDA+Alt+G resolves to script_askAI", repr(script))
			yield from self._gesture(gesture)
			dialog = yield from self._waitFor(dialogs.AskAIDialog.getInstance, 5000)
			if not report(dialog is not None, "Ask dialog opened by NVDA+Alt+G"):
				self._exit()
				return
			yield 1000
			phase("ask-dialog-open")
			yield 800

			# 3. Controls, focus, tab order.
			info("Ask dialog title=%r %s" % (dialog.GetTitle(), msaa(dialog)))
			for index, child in enumerate(walk(dialog)):
				info("Ask control %d: %s" % (index, describe(child)))
			focus = wx.Window.FindFocus()
			report(focus is dialog.questionCtrl, "focus starts in the Question box", describe(focus) if focus else "None")
			nvdaFocus = yield from self._waitFor(
				lambda: api.getFocusObject() if api.getFocusObject().windowHandle == dialog.questionCtrl.GetHandle() else None,
				3000,
			)
			obj = api.getFocusObject()
			report(nvdaFocus is not None, "NVDA focus object is the Question box",
				"name=%r role=%s hwnd=%s" % (obj.name, obj.role.name, obj.windowHandle))
			dupes = {k: v for k, v in mnemonics(dialog).items() if len(v) > 1}
			report(not dupes, "Ask dialog mnemonics unique", repr(mnemonics(dialog)))

			order = []
			tabPath = "SendInput"
			for step in range(25):
				current = wx.Window.FindFocus()
				order.append(current)
				self.sim.Char(wx.WXK_TAB)
				moved = yield from self._waitFor(lambda: wx.Window.FindFocus() is not current, 1000)
				if not moved:
					tabPath = "Navigate()"
					current.Navigate(wx.NavigationKeyEvent.IsForward)
					yield 300
				if wx.Window.FindFocus() is order[0]:
					break
			info("tab order via %s: %s" % (tabPath, " -> ".join(
				"%s(%r)" % (type(w).__name__, w.GetLabel()) if w else "None" for w in order)))
			labels = [w.GetLabel() if w else None for w in order]
			report(labels[:2] == [dialog.questionCtrl.GetLabel(), "&Ask"] and "C&opy last answer" in labels,
				"tab order starts Question, Ask and reaches the bottom buttons", repr(labels))
			report(dialog.projectCtrl in order, "Project folder field is reachable with Tab",
				"AcceptsFocusFromKeyboard=%s" % dialog.projectCtrl.AcceptsFocusFromKeyboard())
			report(all(b in labels for b in ("Save ans&wer as...", "Choose project &folder...", "Conve&rsations...", "&New conversation"))
				and dialog.serviceChoice in order,
				"new buttons and the AI service choice are in the tab order", repr(labels))
			report(dialog.GetSize().width <= 800, "Ask dialog is at most 800 pixels wide", repr(tuple(dialog.GetSize())))
			report(dialog.projectCtrl.GetValue() == "None", "project folder shows None", repr(dialog.projectCtrl.GetValue()))
			dialog.questionCtrl.SetFocus()
			yield 300

			# 4. Type a question and press Enter; the fake Codex answers.
			dialog.questionCtrl.SetFocus()
			self.sim.Text(QUESTION.encode("ascii"))  # wxPython maps the const char* parameter to bytes.
			typed = yield from self._waitFor(lambda: dialog.questionCtrl.GetValue() == QUESTION, 4000)
			if typed:
				info("question typed with wx.UIActionSimulator.Text (SendInput)")
			else:
				info("typing via SendInput gave %r; using SetValue" % dialog.questionCtrl.GetValue())
				dialog.questionCtrl.SetValue(QUESTION)
				dialog.questionCtrl.SetInsertionPointEnd()
			self.sim.Char(wx.WXK_RETURN, wx.MOD_SHIFT)
			newline = yield from self._waitFor(lambda: dialog.questionCtrl.GetValue().endswith("\n"), 2500)
			report(bool(newline) and dialogs.state.pending is None, "Shift+Enter inserts a line break without sending",
				repr(dialog.questionCtrl.GetValue()))
			dialog.questionCtrl.SetValue(QUESTION)
			dialog.questionCtrl.SetInsertionPointEnd()
			phase("question-typed")
			yield 800
			spoken.clear()
			path = yield from self._key(
				dialog, wx.WXK_RETURN, dialog.questionCtrl,
				lambda: dialogs.state.pending is not None or dialogs.state.lastAnswer, "Enter",
			)
			report("\n" not in dialog.questionCtrl.GetValue(), "Enter did not insert a newline", "path=%s" % path)
			done = yield from self._waitFor(lambda: dialogs.state.pending is None and dialogs.state.lastAnswer, 30000)
			yield 500
			phase("answer-shown")
			report(bool(done), "request finished", "lastAnswer=%r" % dialogs.state.lastAnswer)
			report(dialogs.state.lastAnswer == ANSWER_RAW, "raw answer kept for copy/review", repr(dialogs.state.lastAnswer))
			conversation = dialog.conversationCtrl.GetValue()
			info("Conversation box: %r" % conversation)
			report(("ChatGPT: " + ANSWER_SPOKEN) in conversation and "**" not in conversation,
				"answer appears (Markdown stripped) in the Conversation box")
			report(("You: " + QUESTION) in conversation, "question appears in the Conversation box")
			report(ANSWER_SPOKEN in spoken, "ui.message called with the stripped answer", repr(spoken))
			report("Asking ChatGPT\u2026" in spoken, "ui.message announced Asking ChatGPT", repr(spoken))
			report(dialog.questionCtrl.GetValue() == "" and dialog.askButton.GetLabel() == "&Ask",
				"question box cleared and Ask button restored")
			report(wx.Window.FindFocus() is dialog.questionCtrl, "focus stays in the Question box after the answer")
			report(dialog.GetTitle() == "Ask AI - " + QUESTION, "dialog title shows the conversation title", repr(dialog.GetTitle()))
			firstId = dialogs.state.current.id
			saved = store.load(firstId)
			report(saved is not None and saved.lastAnswer == ANSWER_RAW and saved.title == QUESTION,
				"conversation saved as JSON after the answer", repr(saved.toDict() if saved else None))
			calls = readFake("calls.log")
			stdin = readFake("last-stdin.txt")
			info("fake codex calls.log: %r" % calls)
			info("fake codex stdin (sorted lines): %r" % stdin)
			report("ARGS: \"exec\" \"--skip-git-repo-check\"" in calls and "\"-o\"" in calls and calls.count("\"-\"") >= 1,
				"fake codex.cmd ran via the cmd.exe fallback with quoted exec args")
			report(("Question: " + QUESTION) in stdin and "You are a helpful assistant." in stdin,
				"prompt (instructions + question) arrived on stdin")
			report(not glob.glob(os.path.join(dialogs.workDir, "answer-*.txt")), "answer temp file removed",
				repr(os.listdir(dialogs.workDir) if os.path.isdir(dialogs.workDir) else None))
			report(not os.path.exists(os.path.join(dialogs.workDir, "answer-stale.txt")),
				"stale answer file from an earlier crash was cleaned at startup")

			# 4b. Copy last answer copies the raw answer.
			spoken.clear()
			press(next(w for w in walk(dialog) if isinstance(w, wx.Button) and w.GetLabel() == "C&opy last answer"))
			yield from self._waitFor(lambda: spoken, 3000)
			clip = api.getClipData()
			report(clip == ANSWER_RAW and "Last answer copied to the clipboard." in spoken, "Copy last answer",
				"clipboard=%r spoken=%r" % (clip, spoken))
			dialog.questionCtrl.SetFocus()
			yield 300

			# 4c. A Codex 401 error: classified as not signed in, offered to open settings (answer No).
			dialog.questionCtrl.SetValue("FAIL401 question")
			dialog.questionCtrl.SetInsertionPointEnd()
			self.sim.Char(wx.WXK_RETURN)
			offer = yield from self._waitFor(findMessageDialog, 15000)
			if report(offer is not None, "401 error shows a message dialog"):
				yield 500
				phase("error-401-offer")
				yield 600
				texts = [w.GetLabel() for w in walk(offer) if isinstance(w, wx.StaticText)]
				buttons = [w.GetLabel() for w in walk(offer) if isinstance(w, wx.Button)]
				info("401 dialog title=%r text=%r buttons=%r" % (offer.GetTitle(), texts, buttons))
				report(any("not signed in" in t for t in texts), "401 offers to open the sign-in settings", repr(texts))
				noButton = next((w for w in walk(offer) if isinstance(w, wx.Button) and w.GetId() == wx.ID_NO), None)
				press(noButton if noButton is not None else offer.FindWindowById(wx.ID_CANCEL))
				yield from self._waitFor(lambda: findMessageDialog() is None, 5000)
			yield 500
			conversation = dialog.conversationCtrl.GetValue()
			report("Error: Codex is not signed in" in conversation and "Technical details: ERROR: unexpected status 401" in conversation,
				"401 error and technical detail shown in the Conversation box", repr(conversation[-260:]))
			report(dialog.questionCtrl.GetValue() == "FAIL401 question", "failed question restored to the Question box")
			report(findSettingsDialog() is None, "answering No did not open Settings")
			dialog.questionCtrl.SetValue("")
			dialog.questionCtrl.SetFocus()
			yield 300

			# 5. Escape while waiting cancels (and kills the fake through the Job Object); context is sent.
			dialog.questionCtrl.SetValue(SLOW_QUESTION)
			dialog.questionCtrl.SetInsertionPointEnd()
			dialog.questionCtrl.SetFocus()
			yield 300
			spoken.clear()
			beeps.clear()
			path = yield from self._key(dialog, wx.WXK_RETURN, dialog.questionCtrl,
				lambda: dialogs.state.pending is not None, "Enter (slow question)")
			pending = dialogs.state.pending
			started = yield from self._waitFor(lambda: "SLOW-START" in readFake("calls.log"), 10000)
			report(bool(started), "slow request reached the fake codex")
			job = getattr(pending.client, "_job", None) if pending else None
			report(job is not None and bool(job.handle), "running request is in a Windows Job Object", repr(job))
			report(dialog.askButton.GetLabel() == "&Cancel", "Ask button becomes Cancel while busy",
				repr(dialog.askButton.GetLabel()))
			yield 4500
			report(len(beeps) >= 1, "beeps while waiting", "%d beeps" % len(beeps))
			stdin = readFake("last-stdin.txt")
			report("Earlier conversation:" in stdin and ("Assistant: " + ANSWER_RAW) in stdin,
				"follow-up prompt carries the earlier conversation", repr(stdin))
			phase("waiting-slow")
			yield 600
			cancelledAt = time.monotonic()
			path = yield from self._key(dialog, wx.WXK_ESCAPE, dialog.questionCtrl,
				lambda: dialogs.state.pending is None, "Escape (busy)")
			yield 600
			report(dialogs.state.pending is None and dialogs.AskAIDialog.getInstance() is dialog,
				"Escape while waiting cancels and keeps the dialog open", "path=%s" % path)
			report(dialog.questionCtrl.GetValue() == SLOW_QUESTION, "cancelled question restored to the Question box",
				repr(dialog.questionCtrl.GetValue()))
			report("Request cancelled." in spoken, "cancel announced", repr(spoken))
			report(dialog.askButton.GetLabel() == "&Ask", "Ask button restored after cancel")
			processGone = yield from self._waitFor(lambda: pending.client._process is None, 8000)
			report(bool(processGone), "client released the killed process")

			# 6. Escape with nothing pending closes the dialog.
			path = yield from self._key(dialog, wx.WXK_ESCAPE, dialog.questionCtrl,
				lambda: dialogs.AskAIDialog.getInstance() is None, "Escape (idle)")
			report(dialogs.AskAIDialog.getInstance() is None, "Escape when idle closes the dialog", "path=%s" % path)

			# 7. NVDA+Alt+Shift+G repeats the last answer.
			spoken.clear()
			gesture = keyboardHandler.KeyboardInputGesture.fromName("NVDA+alt+shift+g")
			report(getattr(scriptHandler.findScript(gesture), "__name__", "") == "script_speakLastAnswer",
				"NVDA+Alt+Shift+G resolves to script_speakLastAnswer")
			yield from self._gesture(gesture)
			heard = yield from self._waitFor(lambda: ANSWER_SPOKEN in spoken, 3000)
			report(bool(heard), "last answer repeated", repr(spoken))
			# Pressed twice: the raw answer goes to ui.browseableMessage (recorded, not opened: Wine has no MSHTML here).
			browsed = []
			originalBrowseable = ui.browseableMessage
			ui.browseableMessage = lambda *args, **kwargs: browsed.append(args)
			import watchdog

			yield from self._waitFor(lambda: not watchdog.isAttemptingRecovery, 5000)
			inputCore.manager.executeGesture(gesture)
			inputCore.manager.executeGesture(gesture)
			yield from self._waitFor(lambda: browsed, 3000)
			ui.browseableMessage = originalBrowseable
			report(bool(browsed) and browsed[0][:2] == (ANSWER_RAW, "Last answer"),
				"NVDA+Alt+Shift+G twice opens the raw answer in a browseable window", repr(browsed))

			# 8. Reopen through the Tools menu: the conversation is kept and the caret is on the newest answer.
			event = wx.CommandEvent(wx.wxEVT_MENU, plugin.toolsMenuItem.GetId())
			gui.mainFrame.sysTrayIcon.ProcessEvent(event)  # TaskBarIcon is itself the EvtHandler.
			dialog = yield from self._waitFor(dialogs.AskAIDialog.getInstance, 5000)
			if report(dialog is not None, "Tools menu item opens the dialog"):
				yield 800
				phase("reopened")
				ctrl = dialog.conversationCtrl
				position = ctrl.GetInsertionPoint()
				after = ctrl.GetRange(position, ctrl.GetLastPosition())
				report(after.startswith("ChatGPT: " + ANSWER_SPOKEN), "reopened dialog keeps the conversation, caret on the answer",
					"caret=%d text after caret=%r" % (position, after[:60]))
				report(wx.Window.FindFocus() is dialog.questionCtrl, "reopened dialog focuses the Question box")

				# 8b. Dictate: announces, sends Windows+H (which Wine cannot act on), keeps NVDA quiet while text
				# arrives, then restores the exact speech mode and reads what was dictated.
				import speech

				SM = speech.SpeechMode
				conf["dictationSilenceTimeout"] = 2
				dictateButton = next(w for w in walk(dialog) if isinstance(w, wx.Button) and w.GetLabel().startswith("&Dictate"))
				speech.setSpeechMode(SM.talk)
				sent.clear()
				speeches.clear()
				spoken.clear()
				press(dictateButton)
				yield from self._waitFor(lambda: sent or "could not start" in " ".join(map(str, spoken)).lower(), 15000)
				report(any("Voice typing. Speak your question" in s for s in speeches), "Dictate announces voice typing",
					repr(speeches))
				report(len(sent) == 1 and "windows" in sent[0] and sent[0].endswith("h"), "Dictate sends Windows+H while focus is in the Question box",
					"sent=%r spoken=%r active=%s" % (sent, spoken, dialog.IsActive()))
				report(wx.Window.FindFocus() is dialog.questionCtrl, "focus in the Question box after Dictate")
				mode = speech.getState().speechMode
				report(mode == SM.off and dialog._dictation is not None, "NVDA speech is off while dictating", repr(mode))
				yield 600
				dialog.questionCtrl.WriteText("dictated words")  # What voice typing would type.
				restored = yield from self._waitFor(lambda: speech.getState().speechMode == SM.talk, 8000)
				report(bool(restored) and dialog._dictation is None, "speech restored after the silence timeout",
					repr(speech.getState().speechMode))
				report("Dictation finished. dictated words" in spoken, "dictated text read back after dictation", repr(spoken))

				# Toggle: Dictate pressed again stops voice typing and restores the saved mode (here on-demand).
				speech.setSpeechMode(SM.onDemand)
				dialog.questionCtrl.SetValue("")
				dialog.questionCtrl.SetFocus()
				sent.clear()
				spoken.clear()
				press(dictateButton)
				muted = yield from self._waitFor(lambda: dialog._dictation is not None and speech.getState().speechMode == SM.off, 15000)
				report(bool(muted), "dictation mutes from on-demand mode too", repr(speech.getState().speechMode))
				press(dictateButton)
				back = yield from self._waitFor(lambda: speech.getState().speechMode == SM.onDemand, 5000)
				report(bool(back) and len(sent) == 2, "Dictate again sends Windows+H again and restores on-demand mode",
					"mode=%r sent=%r" % (speech.getState().speechMode, sent))
				report("Dictation finished, nothing was typed" in spoken, "nothing dictated is announced", repr(spoken))
				speech.setSpeechMode(SM.talk)

				# Escape while dictating ends dictation only (the dialog stays open).
				dialog.questionCtrl.SetFocus()
				press(dictateButton)
				yield from self._waitFor(lambda: dialog._dictation is not None, 15000)
				path = yield from self._key(dialog, wx.WXK_ESCAPE, dialog.questionCtrl,
					lambda: speech.getState().speechMode == SM.talk, "Escape (dictating)")
				report(speech.getState().speechMode == SM.talk and dialogs.AskAIDialog.getInstance() is dialog,
					"Escape while dictating restores speech and keeps the dialog open", "path=%s" % path)

				# Enter while dictating: speech comes back first, so "Asking ChatGPT" is heard.
				dialog.questionCtrl.SetValue("")
				dialog.questionCtrl.SetFocus()
				press(dictateButton)
				yield from self._waitFor(lambda: dialog._dictation is not None and speech.getState().speechMode == SM.off, 15000)
				dialog.questionCtrl.WriteText("dictated question")
				spokenModes.clear()
				path = yield from self._key(dialog, wx.WXK_RETURN, dialog.questionCtrl,
					lambda: dialogs.state.pending is not None or dialog.questionCtrl.GetValue() == "", "Enter (dictating)")
				report(("Asking ChatGPT\u2026", "talk") in spokenModes, "Enter while dictating restores speech before sending",
					"%r path=%s" % (spokenModes, path))
				yield from self._waitFor(lambda: dialogs.state.pending is None, 20000)
				report(speech.getState().speechMode == SM.talk, "speech is on after dictation")

				# 8c. New conversation: no confirmation (nothing is lost), the old one stays saved.
				spoken.clear()
				press(findButton(dialog, "&New conversation"))
				yield from self._waitFor(lambda: "New conversation started." in spoken, 3000)
				report(findMessageDialog() is None, "New conversation needs no confirmation")
				report(dialogs.state.current.id != firstId and dialogs.state.current.isEmpty and not dialogs.state.lastAnswer
					and dialog.conversationCtrl.GetValue() == "" and dialog.GetTitle() == "Ask AI",
					"New conversation starts empty", repr(spoken))
				old = store.load(firstId)
				report(old is not None and old.lastAnswer == ANSWER_RAW, "the previous conversation stays saved")

				# 8d. Close while waiting: the answer is still spoken after the dialog is gone.
				dialog.questionCtrl.SetValue("MEDIUM question")
				dialog.questionCtrl.SetFocus()
				self.sim.Char(wx.WXK_RETURN)
				yield from self._waitFor(lambda: dialogs.state.pending is not None, 3000)
				spoken.clear()
				press(dialog.FindWindowById(wx.ID_CLOSE))
				closed = yield from self._waitFor(lambda: dialogs.AskAIDialog.getInstance() is None, 3000)
				report(bool(closed) and dialogs.state.pending is not None, "Close button closes the dialog while the request runs")
				report("ChatGPT will speak the answer when it's ready." in spoken, "closing while waiting is announced", repr(spoken))
				# With "Announce when answers finish while working in other apps" (on by default) it starts with "Done: <title>.".
				heard = yield from self._waitFor(lambda: any(str(x).endswith(ANSWER_SPOKEN) for x in spoken), 20000)
				report(bool(heard) and any(str(x) == "Done: MEDIUM question. " + ANSWER_SPOKEN for x in spoken),
					"answer spoken after the dialog was closed, after Done: <title>.", repr(spoken))
				mediumId = dialogs.state.current.id
				yield from self._newFeatures(dialogs, store, firstId, mediumId)
				yield from self._claude(dialogs, store)
				yield from self._round6(dialogs, store)

		# 9. NVDA Settings, Ask AI panel.
		spoken.clear()
		wx.CallAfter(dialogs.openSettings)
		settings = yield from self._waitFor(findSettingsDialog, 8000)
		if not report(settings is not None, "NVDA Settings opened on the Ask AI panel"):
			self._exit()
			return
		panel = settings.currentCategory
		report(isinstance(panel, dialogs.AskAISettingsPanel), "current category is AskAISettingsPanel", repr(panel))
		status = yield from self._waitFor(lambda: panel.statusCtrl.GetValue() == "Signed in with ChatGPT", 15000)
		report(bool(status), "Status shows the fake sign-in", repr(panel.statusCtrl.GetValue()))
		yield 500
		phase("settings-panel")
		yield 800
		info("Settings dialog title=%r" % settings.GetTitle())
		for index, child in enumerate(walk(panel)):
			info("Settings control %d: %s" % (index, describe(child)))
		allMnemonics = mnemonics(settings)
		info("Settings dialog mnemonics: %r" % allMnemonics)
		dupes = {k: v for k, v in allMnemonics.items() if len(v) > 1}
		report(not dupes, "Settings dialog mnemonics unique (panel + dialog buttons)", repr(dupes))
		start = wx.Window.FindFocus()
		info("Settings initial focus: %s" % (describe(start) if start else None))
		order = []
		for step in range(30):
			current = wx.Window.FindFocus()
			order.append(current)
			self.sim.Char(wx.WXK_TAB)
			yield from self._waitFor(lambda: wx.Window.FindFocus() is not current, 1000)
			if wx.Window.FindFocus() is order[0]:
				break
		info("Settings tab order: %s" % " -> ".join(
			"%s(%r/%s)" % (type(w).__name__, w.GetLabel(), msaa(w)) if w else "None" for w in order))
		report(panel.statusCtrl in order, "Status field is reachable with Tab",
			"AcceptsFocusFromKeyboard=%s" % panel.statusCtrl.AcceptsFocusFromKeyboard())
		start.SetFocus()
		yield 300
		browse = next(w for w in walk(panel) if isinstance(w, wx.Button) and w.GetLabel() == "&Browse...")
		# The panel sits in NVDA's scrolled container; compare against what the container shows.
		container = panel.GetParent()
		info("panel size=%r container client=%r browse screen rect=%r container screen rect=%r" % (
			tuple(panel.GetSize()), tuple(container.GetClientSize()), tuple(browse.GetScreenRect()),
			tuple(container.GetScreenRect())))
		report(panel.GetSize().width <= container.GetClientSize().width, "panel fits NVDA's settings area without horizontal scrolling",
			"panel width %d, visible width %d" % (panel.GetSize().width, container.GetClientSize().width))
		checkButton = next(w for w in walk(panel) if isinstance(w, wx.Button) and w.GetLabel() == "C&heck status")
		press(checkButton)
		announced = yield from self._waitFor(lambda: "Signed in with ChatGPT" in spoken, 15000)
		report(bool(announced), "Check status announces the status", repr(spoken))

		# Both services in the settings: the default service and the account section switch.
		names = choiceItems(panel.defaultServiceCtrl)
		report(names == ["ChatGPT (Codex)", "Claude (Claude Code)"] and panel.defaultServiceCtrl.GetSelection() == 0
			and choiceItems(panel.accountCtrl) == names, "Settings offer both services (default and account section)", repr(names))
		report(panel.signInButton.GetLabel() == "&Sign in to ChatGPT..." and not panel.enterCodeButton.IsShown(),
			"ChatGPT account section: Sign in to ChatGPT, no sign-in code button", repr(panel.signInButton.GetLabel()))
		spoken.clear()
		choose(panel.accountCtrl, 1)
		claudeStatus = yield from self._waitFor(lambda: panel.statusCtrl.GetValue() == "Signed in with Claude", 15000)
		report(bool(claudeStatus), "Claude account section shows the fake Claude sign-in", repr(panel.statusCtrl.GetValue()))
		report(panel.signInButton.GetLabel() == "&Sign in to Claude..." and panel.pathLabel.GetLabel().startswith("Claude Code e&xecutable path")
			and panel.pathCtrl.GetValue() == FAKE_CLAUDE and panel.enterCodeButton.IsShown(),
			"Claude account section: labels, path and sign-in code button", repr((panel.signInButton.GetLabel(), panel.pathLabel.GetLabel(),
			panel.pathCtrl.GetValue())))
		claudeModels = [panel.modelCtrl.GetString(i) for i in range(panel.modelCtrl.GetCount())]
		report("Opus (opus)" in claudeModels and choiceItems(panel.effortCtrl)[-1] == "Maximum", "Claude defaults list Claude's models and efforts",
			repr(claudeModels))
		allMnemonics = mnemonics(settings)
		dupes = {k: v for k, v in allMnemonics.items() if len(v) > 1}
		report(not dupes, "Settings mnemonics unique with the Claude section shown", repr(dupes))
		phase("settings-claude")
		yield 500
		panel.modelCtrl.SetValue("Haiku (haiku)")
		choose(panel.accountCtrl, 0)
		yield from self._waitFor(lambda: panel.statusCtrl.GetValue() == "Signed in with ChatGPT", 15000)
		report(panel.pathCtrl.GetValue() == FAKE_CODEX and panel.signInButton.GetLabel() == "&Sign in to ChatGPT...",
			"switching back shows ChatGPT's account and path", repr(panel.pathCtrl.GetValue()))

		modelItems = [panel.modelCtrl.GetString(i) for i in range(panel.modelCtrl.GetCount())]
		report("GPT Test Fast (gpt-test-fast)" in modelItems and "gpt-5-codex" not in modelItems,
			"Settings model box lists Codex's models", repr(modelItems))
		report(panel.projectWritesCheck.GetValue() and panel.dictationTimeoutCtrl.GetValue() == conf["dictationSilenceTimeout"]
			and panel.conversationsDirCtrl.GetValue() == "", "new settings show their values")
		panel.beepCheck.SetValue(False)
		panel.timeoutCtrl.SetValue(45)
		panel.modelCtrl.SetValue("GPT Test Deep (gpt-test-deep)")
		panel.effortCtrl.SetSelection(1)
		panel.projectWritesCheck.SetValue(False)
		press(settings.FindWindowById(wx.ID_OK))
		gone = yield from self._waitFor(lambda: findSettingsDialog() is None, 8000)
		report(bool(gone), "OK closes NVDA Settings")
		conf = config.conf["askAI"]
		saved = (conf["beepWhileWaiting"], conf["timeout"], conf["model"], conf["reasoningEffort"], conf["codexPath"],
			conf["allowProjectWrites"])
		report(saved == (False, 45, "gpt-test-deep", "medium", FAKE_CODEX, False), "OK saved the Ask AI settings", repr(saved))
		report((conf["provider"], conf["claudePath"], conf["claudeModel"]) == ("codex", FAKE_CLAUDE, "haiku"),
			"OK saved the Claude settings edited in its account section", repr((conf["provider"], conf["claudePath"], conf["claudeModel"])))
		config.conf.save()
		iniPath = os.path.join(globalVars.appArgs.configPath, "nvda.ini")
		with open(iniPath, encoding="utf-8") as f:
			ini = f.read()
		section = ini[ini.find("[askAI]"):]
		info("nvda.ini [askAI]: %r" % section[:400])
		report("timeout = 45" in section and "model = gpt-test-deep" in section and "reasoningEffort = medium" in section,
			"settings written to nvda.ini")

		# NVDA refuses to open Settings again while anything still references the destroyed dialog,
		# so drop this test's own references first and see whether anything else keeps it alive.
		import gc
		from gui.settingsDialogs import SettingsDialog

		del panel, settings, checkButton
		yield 500
		gc.collect()
		report(dialogs._activePanel is None, "add-on cleared _activePanel after Settings closed",
			repr(dialogs._activePanel))
		leftovers = [dlg for dlg in list(SettingsDialog._instances) if isinstance(dlg, NVDASettingsDialog)]
		for dlg in leftovers:
			holders = [type(r).__name__ + ":" + repr(r)[:120] for r in gc.get_referrers(dlg) if r is not leftovers]
			info("closed Settings dialog still referenced by %r" % holders)
		report(not leftovers, "closed NVDA Settings dialog was released", repr(leftovers))
		del leftovers

		# 10. An invalid Codex path is refused by isValid().
		wx.CallAfter(dialogs.openSettings)
		settings = yield from self._waitFor(findSettingsDialog, 8000)
		if report(settings is not None, "NVDA Settings reopened"):
			panel = settings.currentCategory
			yield 800
			panel.pathCtrl.SetValue("C:\\no\\such\\codex.exe")
			press(settings.FindWindowById(wx.ID_OK))
			error = yield from self._waitFor(lambda: findTopLevel("Ask AI error"), 5000)
			report(error is not None, "invalid Codex path shows an error")
			if error is not None:
				yield 500
				phase("invalid-path-error")
				yield 800
				texts = [w.GetLabel() for w in walk(error) if isinstance(w, wx.StaticText)]
				info("error dialog %s text=%r" % (type(error).__name__, texts))
				button = next((w for w in walk(error) if isinstance(w, wx.Button)), None)
				if button is not None:
					press(button)
				yield from self._waitFor(lambda: findTopLevel("Ask AI error") is None, 3000)
				if findTopLevel("Ask AI error") is not None:
					error.EndModal(wx.ID_OK) if error.IsModal() else error.Close()
			yield 500
			report(findSettingsDialog() is not None and wx.Window.FindFocus() is panel.pathCtrl,
				"settings stay open with focus on the path field")
			press(settings.FindWindowById(wx.ID_CANCEL))
			yield from self._waitFor(lambda: findSettingsDialog() is None, 5000)
			report(config.conf["askAI"]["codexPath"] == FAKE_CODEX, "Cancel kept the old codexPath")
			del panel, settings

		# 11. The killed slow request never finished.
		remaining = 30 - (time.monotonic() - cancelledAt)
		if remaining > 0:
			yield int(remaining * 1000)
		calls = readFake("calls.log")
		report("SLOW-FINISHED" not in calls, "cancelled fake codex was killed (never reached SLOW-FINISHED)", repr(calls[-300:]))

		# 12. Job Object directly: kill a child tree through it (32-bit structures on this NVDA).
		import subprocess

		probe = subprocess.Popen(
			[codexClient.systemExecutable("cmd.exe"), "/d", "/c", "ping -n 30 127.0.0.1 >nul"],
			creationflags=codexClient.CREATE_NO_WINDOW,
		)
		job = codexClient.WindowsJob()
		assigned = job.assign(probe)
		killed = job.terminate()
		try:
			code = probe.wait(timeout=5)
		except Exception:
			code = None
		job.close()
		report(assigned and killed and code is not None, "WindowsJob assign/terminate kills a cmd.exe child",
			"assign=%s terminate=%s exit=%r" % (assigned, killed, code))

		# 13. Exit NVDA with a slow request running: GlobalPlugin.terminate() must kill it (run.sh checks
		# that the fake never writes a second SLOW-FINISHED and that no ping.exe is left).
		yield from self._gesture(keyboardHandler.KeyboardInputGesture.fromName("NVDA+alt+g"))
		dialog = yield from self._waitFor(dialogs.AskAIDialog.getInstance, 5000)
		if dialog is not None:
			dialog.questionCtrl.SetValue(SLOW_QUESTION)
			dialog.questionCtrl.SetFocus()
			self.sim.Char(wx.WXK_RETURN)
			started = yield from self._waitFor(lambda: readFake("calls.log").count("SLOW-START") >= (1 if focused else 2), 10000)
			report(bool(started), "slow request running at NVDA exit")
		self._exit()
		yield TICK_MS
