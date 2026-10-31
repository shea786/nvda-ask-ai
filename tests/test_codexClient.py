"""Tests for codexClient (no NVDA, no real Codex needed)."""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock

import helpers
from helpers import FakePopen

import codexClient
from codexClient import CodexClient, CodexError

NASTY_QUESTION = 'What is "2 & 3" | echo %PATH% ^ && del C:\\ ; $(rm -rf /) ?'
FAKE_EXE = os.path.abspath("fake-codex")


def makeClient(popen, workDir, **kwargs):
	kwargs.setdefault("isWindows", False)
	return CodexClient(
		executablePath=FAKE_EXE,
		workDir=workDir,
		popen=popen,
		finder=lambda userPath: FAKE_EXE,
		**kwargs,
	)


class TempDirTestCase(unittest.TestCase):
	def setUp(self):
		self.workDir = tempfile.mkdtemp(prefix="askgpt-test-")
		self.addCleanup(shutil.rmtree, self.workDir, True)


class TestAskCommand(TempDirTestCase):
	def test_promptGoesToStdinAndNeverToArgv(self):
		popen = FakePopen(outputText="Four.")
		client = makeClient(popen, self.workDir)
		prompt = codexClient.buildPrompt(NASTY_QUESTION)
		answer = client.ask(prompt)
		self.assertEqual(answer, "Four.")
		argv = popen.lastArgv
		self.assertIsInstance(argv, list)
		joined = " ".join(argv)
		self.assertNotIn("2 & 3", joined)
		self.assertNotIn("PATH", joined)
		self.assertNotIn("Question", joined)
		for arg in argv:
			self.assertNotIn(NASTY_QUESTION, arg)
		self.assertEqual(argv[-1], "-")
		stdinData = popen.lastProcess.stdin.captured.decode("utf-8")
		self.assertEqual(stdinData, prompt)
		self.assertIn(NASTY_QUESTION, stdinData)

	def test_fixedExecArguments(self):
		popen = FakePopen(outputText="ok")
		makeClient(popen, self.workDir, reasoningEffort="").ask("hi")
		argv = popen.lastArgv
		self.assertEqual(argv[0], FAKE_EXE)
		self.assertEqual(argv[1], "exec")
		for flag in ("--skip-git-repo-check", "--sandbox", "read-only", "--color", "never", "-o"):
			self.assertIn(flag, argv)
		self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
		outputFile = argv[argv.index("-o") + 1]
		self.assertEqual(os.path.dirname(outputFile), self.workDir)
		self.assertFalse(os.path.exists(outputFile), "the -o file is removed afterwards")
		kwargs = popen.calls[-1][1]
		self.assertEqual(kwargs["cwd"], self.workDir)
		self.assertEqual(kwargs["stdin"], subprocess.PIPE)
		self.assertNotIn("creationflags", kwargs)

	def test_modelAndEffortFlagsOnlyWhenSet(self):
		popen = FakePopen(outputText="ok")
		makeClient(popen, self.workDir, model="", reasoningEffort="").ask("hi")
		argv = popen.lastArgv
		self.assertNotIn("-m", argv)
		self.assertFalse(any("model_reasoning_effort" in a for a in argv))

		makeClient(popen, self.workDir, model="gpt-5", reasoningEffort="high").ask("hi")
		argv = popen.lastArgv
		self.assertEqual(argv[argv.index("-m") + 1], "gpt-5")
		self.assertIn('model_reasoning_effort="high"', argv)
		self.assertEqual(argv[argv.index('model_reasoning_effort="high"') - 1], "-c")
		self.assertEqual(argv[-1], "-")

	def test_invalidModelRejected(self):
		popen = FakePopen(outputText="ok")
		with self.assertRaises(CodexError) as cm:
			makeClient(popen, self.workDir, model="gpt-5 & calc").ask("hi")
		self.assertEqual(cm.exception.kind, codexClient.KIND_CONFIG)
		self.assertEqual(popen.calls, [])
		with self.assertRaises(CodexError):
			codexClient.buildExecArgs("out.txt", reasoningEffort="extreme")

	def test_windowsCreationFlags(self):
		popen = FakePopen(outputText="ok")
		client = makeClient(popen, self.workDir, isWindows=True)
		client.ask("hi")
		self.assertEqual(popen.calls[-1][1]["creationflags"], codexClient.CREATE_NO_WINDOW)

	def test_outputFileWinsOverStdout(self):
		popen = FakePopen(outputText="  From file.  ", stdout=b"From stdout")
		self.assertEqual(makeClient(popen, self.workDir).ask("hi"), "From file.")

	def test_stdoutFallbackWhenOutputFileEmpty(self):
		popen = FakePopen(outputText="", stdout="  Caf\u00e9 answer \n".encode("utf-8"))
		self.assertEqual(makeClient(popen, self.workDir).ask("hi"), "Caf\u00e9 answer")

	def test_invalidUtf8IsReplaced(self):
		popen = FakePopen(outputText="", stdout=b"bad \xff byte")
		self.assertEqual(makeClient(popen, self.workDir).ask("hi"), "bad \ufffd byte")

	def test_emptyAnswerIsError(self):
		popen = FakePopen(outputText="", stdout=b"   ")
		with self.assertRaises(CodexError) as cm:
			makeClient(popen, self.workDir).ask("hi")
		self.assertEqual(cm.exception.kind, codexClient.KIND_OTHER)

	def test_failureIsClassified(self):
		popen = FakePopen(returncode=1, stderr=b"ERROR: unexpected status 401 Unauthorized: Missing bearer")
		with self.assertRaises(CodexError) as cm:
			makeClient(popen, self.workDir).ask("hi")
		self.assertEqual(cm.exception.kind, codexClient.KIND_NOT_LOGGED_IN)


class TestProcessControl(TempDirTestCase):
	def test_timeoutKillsProcess(self):
		popen = FakePopen(hang=True)
		client = makeClient(popen, self.workDir)
		client.timeout = 0.3  # Below the user-facing minimum, for a fast test.
		with self.assertRaises(CodexError) as cm:
			client.ask("hi")
		self.assertEqual(cm.exception.kind, codexClient.KIND_TIMEOUT)
		self.assertTrue(popen.lastProcess.killed)

	def test_cancelKillsProcess(self):
		popen = FakePopen(hang=True)
		client = makeClient(popen, self.workDir)
		cancelEvent = threading.Event()
		timer = threading.Timer(0.2, cancelEvent.set)
		timer.start()
		self.addCleanup(timer.cancel)
		with self.assertRaises(CodexError) as cm:
			client.ask("hi", cancelEvent=cancelEvent)
		self.assertEqual(cm.exception.kind, codexClient.KIND_CANCELLED)
		self.assertTrue(popen.lastProcess.killed)

	def test_windowsKillUsesTaskkillTree(self):
		proc = helpers.FakeProcess(["x"], hang=True)
		run = mock.Mock()
		codexClient.killProcessTree(proc, isWindows=True, run=run)
		argv = run.call_args[0][0]
		self.assertEqual(argv, ["taskkill", "/T", "/F", "/PID", "4242"])
		self.assertTrue(proc.killed)

	def test_killSkipsFinishedProcess(self):
		proc = helpers.FakeProcess(["x"], returncode=0)
		run = mock.Mock()
		codexClient.killProcessTree(proc, isWindows=True, run=run)
		run.assert_not_called()
		self.assertFalse(proc.killed)

	def test_missingExecutable(self):
		client = CodexClient(workDir=self.workDir, popen=FakePopen(), finder=lambda userPath: None)
		with self.assertRaises(CodexError) as cm:
			client.ask("hi")
		self.assertEqual(cm.exception.kind, codexClient.KIND_NOT_INSTALLED)

	def test_popenFileNotFound(self):
		def popen(*args, **kwargs):
			raise FileNotFoundError("nope")

		client = makeClient(popen, self.workDir)
		with self.assertRaises(CodexError) as cm:
			client.ask("hi")
		self.assertEqual(cm.exception.kind, codexClient.KIND_NOT_INSTALLED)

	def test_loginStartsWithoutWaiting(self):
		popen = FakePopen(hang=True)
		client = makeClient(popen, self.workDir, isWindows=True)
		proc = client.login()
		self.assertIs(proc, popen.lastProcess)
		self.assertEqual(popen.lastArgv, [FAKE_EXE, "login"])
		kwargs = popen.calls[-1][1]
		self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
		self.assertTrue(kwargs["creationflags"] & codexClient.CREATE_NO_WINDOW)
		with mock.patch.object(codexClient.subprocess, "run"):
			client.cancelLogin()
		self.assertTrue(proc.killed)

	def test_loginStatusLogoutVersion(self):
		popen = FakePopen(returncode=0, stdout=b"Logged in using ChatGPT\n")
		client = makeClient(popen, self.workDir)
		self.assertEqual(client.loginStatus(), (True, "Logged in using ChatGPT"))
		self.assertEqual(popen.lastArgv, [FAKE_EXE, "login", "status"])
		self.assertEqual(client.logout()[0], True)
		self.assertEqual(popen.lastArgv, [FAKE_EXE, "logout"])
		popen = FakePopen(returncode=0, stdout=b"codex-cli 0.160.0\n")
		self.assertEqual(makeClient(popen, self.workDir).version(), "codex-cli 0.160.0")
		self.assertEqual(popen.lastArgv, [FAKE_EXE, "--version"])


class TestCommandBuilding(unittest.TestCase):
	def test_cmdShimWrappedInCmdExe(self):
		args = codexClient.buildExecArgs(r"C:\Users\Jo Smith\AppData\Roaming\nvda\askGPT-workdir\answer-1.txt")
		command = codexClient.buildCommand(
			r"C:\Users\Jo Smith\AppData\Roaming\npm\codex.cmd",
			args,
			isWindows=True,
			resolveShim=lambda path: None,
		)
		self.assertIsInstance(command, str)
		self.assertTrue(command.startswith('cmd.exe /d /s /c ""C:\\Users\\Jo Smith\\AppData\\Roaming\\npm\\codex.cmd" exec '))
		self.assertTrue(command.endswith(' -"'))
		self.assertIn('"C:\\Users\\Jo Smith\\AppData\\Roaming\\nvda\\askGPT-workdir\\answer-1.txt"', command)

	def test_cmdWrapperRejectsUnsafePaths(self):
		with self.assertRaises(CodexError):
			codexClient.buildCommand(r"C:\%evil%\codex.cmd", ["exec"], isWindows=True, resolveShim=lambda p: None)

	def test_resolvedShimRunsNodeDirectly(self):
		command = codexClient.buildCommand(
			r"C:\npm\codex.cmd",
			["exec", "-"],
			isWindows=True,
			resolveShim=lambda path: [r"C:\node\node.exe", r"C:\npm\node_modules\@openai\codex\bin\codex.js"],
		)
		self.assertEqual(
			command,
			[r"C:\node\node.exe", r"C:\npm\node_modules\@openai\codex\bin\codex.js", "exec", "-"],
		)

	def test_exeIsRunDirectly(self):
		self.assertEqual(
			codexClient.buildCommand(r"C:\bin\codex.exe", ["exec"], isWindows=True),
			[r"C:\bin\codex.exe", "exec"],
		)

	def test_resolveCmdShim(self):
		shim = textwrap.dedent(
			r"""
			@ECHO off
			SETLOCAL
			IF EXIST "%dp0%\node.exe" (SET "_prog=%dp0%\node.exe") ELSE (SET "_prog=node")
			endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@openai\codex\bin\codex.js" %*
			""",
		)
		base = os.path.join("npmdir")
		cmdPath = os.path.join(base, "codex.cmd")
		script = os.path.join(base, "node_modules", "@openai", "codex", "bin", "codex.js")
		existing = {script}
		result = codexClient.resolveCmdShim(
			cmdPath,
			readText=lambda p: shim,
			which=lambda name: "/usr/bin/node",
			isFile=lambda p: p in existing,
		)
		self.assertEqual(result, ["/usr/bin/node", script])
		existing.add(os.path.join(base, "node.exe"))
		result = codexClient.resolveCmdShim(cmdPath, readText=lambda p: shim, which=lambda n: None, isFile=lambda p: p in existing)
		self.assertEqual(result, [os.path.join(base, "node.exe"), script])
		self.assertIsNone(
			codexClient.resolveCmdShim(cmdPath, readText=lambda p: "@echo off", which=lambda n: "node", isFile=lambda p: True),
		)


class TestFindCodex(unittest.TestCase):
	def test_userPathWins(self):
		self.assertEqual(
			codexClient.findCodex('"C:\\x\\codex.exe"', which=lambda n: "other", isFile=lambda p: True),
			"C:\\x\\codex.exe",
		)
		self.assertIsNone(codexClient.findCodex("C:\\missing.exe", which=lambda n: "other", isFile=lambda p: False))

	def test_pathLookup(self):
		self.assertEqual(
			codexClient.findCodex("", which=lambda n: "/usr/bin/codex", isFile=lambda p: False, isWindows=False),
			"/usr/bin/codex",
		)

	def test_windowsExtensionlessShimPrefersCmd(self):
		found = codexClient.findCodex(
			"",
			env={},
			which=lambda n: r"C:\npm\codex",
			isFile=lambda p: p == r"C:\npm\codex.cmd",
			isWindows=True,
		)
		self.assertEqual(found, r"C:\npm\codex.cmd")

	def test_windowsWellKnownLocations(self):
		env = {"APPDATA": "AD", "LOCALAPPDATA": "LAD", "USERPROFILE": "UP"}
		candidates = codexClient.candidatePaths(env, isWindows=True)
		self.assertEqual(candidates[0], os.path.join("AD", "npm", "codex.cmd"))
		self.assertIn(os.path.join("UP", ".local", "bin", "codex.exe"), candidates)
		self.assertTrue(any(c.startswith(os.path.join("LAD", "Programs")) for c in candidates))
		target = os.path.join("UP", ".local", "bin", "codex.exe")
		found = codexClient.findCodex("", env=env, which=lambda n: None, isFile=lambda p: p == target, isWindows=True)
		self.assertEqual(found, target)
		self.assertIsNone(codexClient.findCodex("", env=env, which=lambda n: None, isFile=lambda p: False, isWindows=True))


class TestClassifyFailure(unittest.TestCase):
	def assertKind(self, kind, stderr, stdout="", prompt=""):
		error = codexClient.classifyFailure(1, stdout, stderr, prompt)
		self.assertEqual(error.kind, kind, stderr)
		return error

	def test_notLoggedIn(self):
		# Real output of codex-cli 0.160.0 with no credentials.
		self.assertKind(
			codexClient.KIND_NOT_LOGGED_IN,
			"ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication in header",
		)
		self.assertKind(codexClient.KIND_NOT_LOGGED_IN, "Not logged in")
		self.assertKind(codexClient.KIND_NOT_LOGGED_IN, "", stdout="Please sign in with ChatGPT")
		self.assertKind(codexClient.KIND_NOT_LOGGED_IN, "Run codex login first")

	def test_usageLimit(self):
		self.assertKind(codexClient.KIND_USAGE_LIMIT, "You've hit your usage limit. Try again at 3:05 PM.")
		self.assertKind(codexClient.KIND_USAGE_LIMIT, "ERROR: unexpected status 429 Too Many Requests")
		self.assertKind(codexClient.KIND_USAGE_LIMIT, "stream error: rate limit exceeded")

	def test_notInstalled(self):
		self.assertKind(
			codexClient.KIND_NOT_INSTALLED,
			"'node' is not recognized as an internal or external command,\noperable program or batch file.",
		)

	def test_otherIncludesStderrTail(self):
		stderr = "x" * 1000 + "model gpt-9 is not supported"
		error = self.assertKind(codexClient.KIND_OTHER, stderr)
		self.assertTrue(error.message.endswith("model gpt-9 is not supported"))
		self.assertLessEqual(len(error.detail), 300)

	def test_tokenCountsAreNotStatusCodes(self):
		self.assertKind(codexClient.KIND_OTHER, "tokens used\n28,401\nERROR: stream disconnected")
		self.assertKind(codexClient.KIND_OTHER, "tokens used 1,429\nERROR: boom")

	def test_echoedPromptDoesNotSkewClassification(self):
		prompt = codexClient.buildPrompt("How do I log in to my bank? It said 401 unauthorized.")
		stderr = "OpenAI Codex\nuser\n" + prompt + "\nERROR: stream disconnected before completion"
		self.assertKind(codexClient.KIND_OTHER, stderr, prompt=prompt)


class TestLoginStatusParsing(unittest.TestCase):
	def test_parse(self):
		self.assertEqual(codexClient.parseLoginStatus(0, "Logged in using ChatGPT\n"), (True, "Logged in using ChatGPT"))
		loggedIn, text = codexClient.parseLoginStatus(
			1,
			"WARNING: proceeding, even though we could not create PATH aliases\nNot logged in\n",
		)
		self.assertFalse(loggedIn)
		self.assertEqual(text, "Not logged in")
		self.assertFalse(codexClient.parseLoginStatus(0, "Not logged in")[0])
		self.assertFalse(codexClient.parseLoginStatus(1, "Logged in using ChatGPT")[0])

	def test_apiKeyLogin(self):
		self.assertTrue(codexClient.isApiKeyLogin("Logged in using an API key - sk-proj-***"))
		self.assertFalse(codexClient.isApiKeyLogin("Logged in using ChatGPT"))


class TestBuildPrompt(unittest.TestCase):
	def test_withoutHistory(self):
		prompt = codexClient.buildPrompt("  Why is the sky blue? ")
		self.assertTrue(prompt.startswith(codexClient.DEFAULT_INSTRUCTIONS))
		self.assertNotIn("Earlier conversation:", prompt)
		self.assertTrue(prompt.rstrip().endswith("Question: Why is the sky blue?"))
		self.assertIn("Do not run commands", prompt)
		self.assertIn("The user is blind and uses a screen reader.", prompt)

	def test_blankInstructionsUseDefault(self):
		self.assertTrue(codexClient.buildPrompt("q", instructions="  ").startswith(codexClient.DEFAULT_INSTRUCTIONS))
		self.assertTrue(codexClient.buildPrompt("q", instructions="Be brief.").startswith("Be brief."))

	def test_historyFormatAndOrder(self):
		history = [("user", "What is 2+2?"), ("assistant", "4."), ("user", "And times 3?"), ("assistant", "12.")]
		prompt = codexClient.buildPrompt("And minus 1?", history, "Rules.")
		self.assertEqual(
			prompt,
			"Rules.\n\nEarlier conversation:\nYou: What is 2+2?\nGPT: 4.\nYou: And times 3?\nGPT: 12.\n\n"
			"Question: And minus 1?\n",
		)

	def test_turnLimit(self):
		history = []
		for i in range(15):
			history.append(("user", "question %d" % i))
			history.append(("assistant", "answer %d" % i))
		prompt = codexClient.buildPrompt("next", history, maxTurns=10)
		self.assertNotIn("question 4\n", prompt)
		self.assertNotIn("answer 4\n", prompt)
		self.assertIn("You: question 5\n", prompt)
		self.assertIn("GPT: answer 14\n", prompt)

	def test_characterLimitDropsOldestFirst(self):
		history = [("user", "old " + "a" * 5000), ("assistant", "mid " + "b" * 5000), ("user", "new " + "c" * 5000)]
		prompt = codexClient.buildPrompt("q", history, maxChars=12000)
		self.assertNotIn("old ", prompt)
		self.assertIn("mid ", prompt)
		self.assertIn("new ", prompt)

	def test_singleHugeTurnIsClipped(self):
		history = [("assistant", "start " + "z" * 50000 + " end")]
		prompt = codexClient.buildPrompt("q", history, maxChars=12000)
		self.assertIn(" end", prompt)
		self.assertNotIn("start ", prompt)
		self.assertLess(len(prompt), 13000)


@unittest.skipIf(os.name == "nt", "uses a POSIX script as a stand-in for codex")
class TestRealSubprocess(TempDirTestCase):
	"""End to end with a real child process that behaves like codex exec."""

	def setUp(self):
		super().setUp()
		self.exe = os.path.join(self.workDir, "codex")
		with open(self.exe, "w", encoding="utf-8") as f:
			f.write(
				"#!%s\n" % sys.executable
				+ textwrap.dedent(
					"""
					import sys, time
					args = sys.argv[1:]
					if args[:1] == ["--version"]:
						print("codex-cli 9.9.9"); sys.exit(0)
					prompt = sys.stdin.buffer.read().decode("utf-8")
					if "SLEEP" in prompt:
						time.sleep(30)
					sys.stderr.write("user\\n" + prompt + "\\n")
					out = args[args.index("-o") + 1]
					with open(out, "w", encoding="utf-8") as f:
						f.write("ECHO:" + prompt.strip().splitlines()[-1])
					""",
				),
			)
		os.chmod(self.exe, os.stat(self.exe).st_mode | stat.S_IEXEC)

	def test_roundTrip(self):
		client = CodexClient(executablePath=self.exe, workDir=self.workDir, isWindows=False)
		self.assertEqual(client.version(), "codex-cli 9.9.9")
		answer = client.ask(codexClient.buildPrompt("caf\u00e9 \"quotes\" & pipes |"))
		self.assertEqual(answer, "ECHO:Question: caf\u00e9 \"quotes\" & pipes |")

	def test_realTimeout(self):
		client = CodexClient(executablePath=self.exe, workDir=self.workDir, isWindows=False)
		client.timeout = 1
		with self.assertRaises(CodexError) as cm:
			client.ask("SLEEP please")
		self.assertEqual(cm.exception.kind, codexClient.KIND_TIMEOUT)


if __name__ == "__main__":
	unittest.main()
