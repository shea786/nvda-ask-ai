"""Tests for codexClient (no NVDA, no real Codex needed)."""

import ctypes
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

import helpers
from helpers import FakeJob, FakePopen

import codexClient
from codexClient import CodexClient, CodexError

NASTY_QUESTION = 'What is "2 & 3" | echo %PATH% ^ && del C:\\ ; $(rm -rf /) ?'
FAKE_EXE = os.path.abspath("fake-codex")
WIN_ENV = {"SystemRoot": "C:\\Windows", "ProgramFiles": "C:\\Program Files"}


def makeClient(popen, workDir, **kwargs):
	kwargs.setdefault("isWindows", False)
	kwargs.setdefault("killpg", mock.Mock())
	if kwargs["isWindows"]:
		kwargs.setdefault("jobFactory", FakeJob)
		kwargs.setdefault("env", WIN_ENV)
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
		# C1: no inner quotes in -c values (Codex treats non-TOML values as strings).
		self.assertIn("approval_policy=never", argv)
		self.assertFalse(any('"' in arg for arg in argv))
		outputFile = argv[argv.index("-o") + 1]
		self.assertEqual(os.path.dirname(outputFile), self.workDir)
		self.assertFalse(os.path.exists(outputFile), "the -o file is removed afterwards")
		kwargs = popen.calls[-1][1]
		self.assertEqual(kwargs["cwd"], self.workDir)
		self.assertEqual(kwargs["stdin"], subprocess.PIPE)
		self.assertNotIn("creationflags", kwargs)
		# C10: own process group so the whole tree can be killed.
		self.assertTrue(kwargs["start_new_session"])

	def test_modelAndEffortFlagsOnlyWhenSet(self):
		popen = FakePopen(outputText="ok")
		makeClient(popen, self.workDir, model="", reasoningEffort="").ask("hi")
		argv = popen.lastArgv
		self.assertNotIn("-m", argv)
		self.assertFalse(any("model_reasoning_effort" in a for a in argv))

		makeClient(popen, self.workDir, model="gpt-5", reasoningEffort="high").ask("hi")
		argv = popen.lastArgv
		self.assertEqual(argv[argv.index("-m") + 1], "gpt-5")
		self.assertIn("model_reasoning_effort=high", argv)
		self.assertEqual(argv[argv.index("model_reasoning_effort=high") - 1], "-c")
		self.assertEqual(argv[-1], "-")

	def test_invalidModelRejected(self):
		popen = FakePopen(outputText="ok")
		with self.assertRaises(CodexError) as cm:
			makeClient(popen, self.workDir, model="gpt-5 & calc").ask("hi")
		self.assertEqual(cm.exception.kind, codexClient.KIND_CONFIG)
		self.assertEqual(popen.calls, [])
		with self.assertRaises(CodexError):
			codexClient.buildExecArgs("out.txt", reasoningEffort="extreme")

	def test_windowsCreationFlagsAndJob(self):
		FakeJob.instances.clear()
		popen = FakePopen(outputText="ok")
		client = makeClient(popen, self.workDir, isWindows=True)
		client.ask("hi")
		kwargs = popen.calls[-1][1]
		self.assertEqual(kwargs["creationflags"], codexClient.CREATE_NO_WINDOW)
		self.assertNotIn("start_new_session", kwargs)
		job = FakeJob.instances[-1]
		self.assertEqual(job.assigned, [popen.lastProcess])
		self.assertTrue(job.closed, "the job handle is released after the run")

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
		self.assertEqual(cm.exception.reason, codexClient.REASON_EMPTY_ANSWER)

	def test_emptyAnswerWithExitZeroIsClassified(self):
		# C7: Codex can exit 0 after an error; the error must be reported, not "empty answer".
		popen = FakePopen(returncode=0, outputText="", stderr=b"ERROR: unexpected status 401 Unauthorized")
		with self.assertRaises(CodexError) as cm:
			makeClient(popen, self.workDir).ask("hi")
		self.assertEqual(cm.exception.kind, codexClient.KIND_NOT_LOGGED_IN)

	def test_failureIsClassified(self):
		popen = FakePopen(returncode=1, stderr=b"ERROR: unexpected status 401 Unauthorized: Missing bearer")
		with self.assertRaises(CodexError) as cm:
			makeClient(popen, self.workDir).ask("hi")
		self.assertEqual(cm.exception.kind, codexClient.KIND_NOT_LOGGED_IN)

	def test_answerFileRemovalIsRetried(self):
		# C6: Windows may still hold the -o file briefly after the process exits.
		popen = FakePopen(outputText="ok")
		realRemove = os.remove
		calls = []

		def flakyRemove(path):
			calls.append(path)
			if len(calls) == 1:
				raise PermissionError("in use")
			realRemove(path)

		with mock.patch.object(codexClient.os, "remove", flakyRemove), mock.patch.object(codexClient.time, "sleep"):
			makeClient(popen, self.workDir).ask("hi")
		self.assertEqual(len(calls), 2)
		self.assertEqual(os.listdir(self.workDir), [])


class TestProcessControl(TempDirTestCase):
	def test_timeoutKillsProcessGroup(self):
		popen = FakePopen(hang=True)
		client = makeClient(popen, self.workDir)
		client.timeout = 0.3  # Below the user-facing minimum, for a fast test.
		with self.assertRaises(CodexError) as cm:
			client.ask("hi")
		self.assertEqual(cm.exception.kind, codexClient.KIND_TIMEOUT)
		self.assertTrue(popen.lastProcess.killed)
		client._killpg.assert_called_once_with(4242, codexClient.SIGKILL)

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

	def test_windowsTimeoutUsesJob(self):
		# C5: the Job Object kills the whole tree; taskkill is not needed.
		FakeJob.instances.clear()
		popen = FakePopen(hang=True)
		client = makeClient(popen, self.workDir, isWindows=True)
		client.timeout = 0.3
		with mock.patch.object(codexClient.subprocess, "run") as run:
			with self.assertRaises(CodexError):
				client.ask("hi")
		run.assert_not_called()
		job = FakeJob.instances[-1]
		self.assertTrue(job.terminated)
		self.assertTrue(job.closed)
		self.assertTrue(popen.lastProcess.killed)

	def test_windowsFallsBackToTaskkillWhenJobFails(self):
		FakeJob.instances.clear()
		popen = FakePopen(hang=True)
		client = makeClient(popen, self.workDir, isWindows=True, jobFactory=lambda: FakeJob(assignResult=False))
		client.timeout = 0.3
		with mock.patch.object(codexClient.subprocess, "run") as run:
			with self.assertRaises(CodexError):
				client.ask("hi")
		self.assertEqual(run.call_args[0][0][0], "C:\\Windows\\System32\\taskkill.exe")
		self.assertTrue(FakeJob.instances[-1].closed, "an unassigned job is closed at once")

	def test_jobFactoryErrorFallsBack(self):
		def brokenFactory():
			raise OSError("no jobs here")

		popen = FakePopen(outputText="ok")
		self.assertEqual(makeClient(popen, self.workDir, isWindows=True, jobFactory=brokenFactory).ask("hi"), "ok")

	def test_terminateKillsRunningRequestSynchronously(self):
		# C5: GlobalPlugin.terminate() must not leave codex running.
		FakeJob.instances.clear()
		popen = FakePopen(hang=True)
		client = makeClient(popen, self.workDir, isWindows=True)
		errors = []

		def worker():
			try:
				client.ask("hi")
			except CodexError as e:
				errors.append(e)

		thread = threading.Thread(target=worker)
		thread.start()
		for _ in range(100):
			if popen.processes:
				break
			time.sleep(0.01)
		client.terminate()
		self.assertTrue(popen.lastProcess.killed, "killed before terminate() returns")
		self.assertTrue(FakeJob.instances[-1].terminated)
		thread.join(5)
		self.assertFalse(thread.is_alive())
		self.assertEqual(len(errors), 1)

	def test_windowsKillUsesAbsoluteTaskkill(self):
		proc = helpers.FakeProcess(["x"], hang=True)
		run = mock.Mock()
		codexClient.killProcessTree(proc, isWindows=True, run=run, env=WIN_ENV)
		argv = run.call_args[0][0]
		self.assertEqual(argv, ["C:\\Windows\\System32\\taskkill.exe", "/T", "/F", "/PID", "4242"])
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
		self.assertEqual(cm.exception.reason, "")

	def test_missingCustomPath(self):
		# A4: a wrong configured path gets its own message, naming the path.
		client = CodexClient(executablePath="C:\\nope\\codex.exe", workDir=self.workDir, finder=lambda userPath: None)
		with self.assertRaises(CodexError) as cm:
			client.locate()
		self.assertEqual(cm.exception.reason, codexClient.REASON_CUSTOM_PATH)
		self.assertEqual(cm.exception.detail, "C:\\nope\\codex.exe")

	def test_popenFileNotFound(self):
		def popen(*args, **kwargs):
			raise FileNotFoundError("nope")

		client = makeClient(popen, self.workDir)
		with self.assertRaises(CodexError) as cm:
			client.ask("hi")
		self.assertEqual(cm.exception.kind, codexClient.KIND_NOT_INSTALLED)

	def test_loginCapturesSignInLink(self):
		# A3: Codex prints the link on stderr, which is merged into stdout and read in a thread.
		FakeJob.instances.clear()
		output = (
			b"Starting local login server on http://localhost:1455.\n"
			b"If your browser did not open, navigate to this URL to authenticate:\n\n"
			b"https://auth.openai.com/oauth/authorize?response_type=code&state=abc\n"
		)
		popen = FakePopen(hang=True, stdout=output)
		client = makeClient(popen, self.workDir, isWindows=True)
		seen = []
		proc = client.login(onUrl=seen.append)
		self.assertIs(proc, popen.lastProcess)
		self.assertEqual(popen.lastArgv, [FAKE_EXE, "login"])
		kwargs = popen.calls[-1][1]
		self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
		self.assertEqual(kwargs["stdout"], subprocess.PIPE)
		self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
		self.assertTrue(kwargs["creationflags"] & codexClient.CREATE_NO_WINDOW)
		for _ in range(100):
			if seen:
				break
			time.sleep(0.01)
		url = "https://auth.openai.com/oauth/authorize?response_type=code&state=abc"
		self.assertEqual(seen, [url])
		self.assertEqual(client.loginUrl, url)
		job = FakeJob.instances[-1]
		self.assertEqual(job.assigned, [proc])
		client.cancelLogin()
		self.assertTrue(proc.killed)
		self.assertTrue(job.terminated and job.closed)

	def test_newLoginKillsPreviousOne(self):
		popen = FakePopen(hang=True)
		client = makeClient(popen, self.workDir)
		first = client.login()
		second = client.login()
		self.assertTrue(first.killed)
		self.assertFalse(second.killed)
		client.terminate()
		self.assertTrue(second.killed)

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

	def test_extractUrl(self):
		self.assertEqual(codexClient.extractUrl("go to https://a.example/x?y=1."), "https://a.example/x?y=1")
		self.assertIsNone(codexClient.extractUrl("server on http://localhost:1455"))


class TestJobObjectDeclarations(unittest.TestCase):
	"""C5: the ctypes structures must match the Windows layout for the running pointer size."""

	def test_structureLayout(self):
		ext = codexClient.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
		basic = codexClient.JOBOBJECT_BASIC_LIMIT_INFORMATION
		self.assertEqual(ctypes.sizeof(codexClient.IO_COUNTERS), 48)
		self.assertEqual(basic.LimitFlags.offset, 16)
		if ctypes.sizeof(ctypes.c_void_p) == 8:
			self.assertEqual(ctypes.sizeof(basic), 64)
			self.assertEqual(ctypes.sizeof(ext), 144)
			self.assertEqual(ext.IoInfo.offset, 64)
			self.assertEqual(ext.ProcessMemoryLimit.offset, 112)

	def test_windowsJobWithFakeKernel32(self):
		kernel32 = mock.Mock()
		kernel32.CreateJobObjectW.return_value = 1234
		kernel32.SetInformationJobObject.return_value = 1
		kernel32.AssignProcessToJobObject.return_value = 1
		kernel32.TerminateJobObject.return_value = 1
		job = codexClient.WindowsJob(kernel32)
		args = kernel32.SetInformationJobObject.call_args[0]
		self.assertEqual(args[0], 1234)
		self.assertEqual(args[1], 9)
		info = args[2]._obj
		self.assertEqual(info.BasicLimitInformation.LimitFlags, 0x2000)
		self.assertEqual(args[3], ctypes.sizeof(codexClient.JOBOBJECT_EXTENDED_LIMIT_INFORMATION))
		proc = helpers.FakeProcess(["x"])
		self.assertTrue(job.assign(proc))
		kernel32.AssignProcessToJobObject.assert_called_with(1234, 777)
		self.assertTrue(job.terminate())
		job.close()
		kernel32.CloseHandle.assert_called_once_with(1234)
		job.close()
		kernel32.CloseHandle.assert_called_once()

	def test_windowsJobSetupFailureClosesHandle(self):
		kernel32 = mock.Mock()
		kernel32.CreateJobObjectW.return_value = 99
		kernel32.SetInformationJobObject.return_value = 0
		with self.assertRaises(OSError):
			codexClient.WindowsJob(kernel32)
		kernel32.CloseHandle.assert_called_once_with(99)

	def test_moduleImportsWithoutWindowsDlls(self):
		# Loading kernel32 is deferred, so importing on Linux works and the default job factory is lazy.
		self.assertIsNone(codexClient._kernel32)


class TestCommandBuilding(unittest.TestCase):
	def test_cmdFallbackQuotesEveryArgument(self):
		# C1: & | < > ^ in paths must be inside quotes; cmd.exe is called by absolute path with /v:off.
		outputFile = "C:\\Users\\R&D <Team>\\AppData\\Local\\askGPT\\workdir\\answer-1.txt"
		args = codexClient.buildExecArgs(outputFile, reasoningEffort="low")
		command = codexClient.buildCommand(
			"C:\\Users\\R&D <Team>\\npm\\co^dex|x.cmd",
			args,
			isWindows=True,
			resolveShim=lambda path: None,
			env=WIN_ENV,
		)
		self.assertIsInstance(command, str)
		prefix = '"C:\\Windows\\System32\\cmd.exe" /d /v:off /s /c "'
		self.assertTrue(command.startswith(prefix), command)
		self.assertTrue(command.endswith('"'))
		inner = command[len(prefix):-1]
		self.assertTrue(inner.startswith('"C:\\Users\\R&D <Team>\\npm\\co^dex|x.cmd" "exec" '))
		self.assertIn('"%s"' % outputFile, inner)
		self.assertIn('"approval_policy=never"', inner)
		self.assertIn('"model_reasoning_effort=low"', inner)
		self.assertTrue(inner.endswith(' "-"'))
		# Every special character sits between an even number of preceding quotes' opening and closing.
		for index, ch in enumerate(inner):
			if ch in "&|<>^":
				self.assertEqual(inner[:index].count('"') % 2, 1, "unquoted %r at %d" % (ch, index))

	def test_cmdFallbackRejectsUnsafeArguments(self):
		for bad in ('C:\\a"b\\codex.cmd', "C:\\%evil%\\codex.cmd", "C:\\a\nb.cmd", "C:\\a\rb.cmd"):
			with self.assertRaises(CodexError, msg=repr(bad)):
				codexClient.buildCommand(bad, ["exec"], isWindows=True, resolveShim=lambda p: None, env=WIN_ENV)
		with self.assertRaises(CodexError):
			codexClient.quoteForCmd(["C:\\dir\\"])

	def test_resolvedShimRunsNodeDirectly(self):
		command = codexClient.buildCommand(
			"C:\\npm\\codex.cmd",
			["exec", "-"],
			isWindows=True,
			resolveShim=lambda path: ["C:\\node\\node.exe", "C:\\npm\\node_modules\\@openai\\codex\\bin\\codex.js"],
		)
		self.assertEqual(
			command,
			["C:\\node\\node.exe", "C:\\npm\\node_modules\\@openai\\codex\\bin\\codex.js", "exec", "-"],
		)

	def test_exeIsRunDirectly(self):
		self.assertEqual(
			codexClient.buildCommand("C:\\bin\\codex.exe", ["exec"], isWindows=True),
			["C:\\bin\\codex.exe", "exec"],
		)


SHIM = textwrap.dedent(
	r"""
	@ECHO off
	SETLOCAL
	IF EXIST "%dp0%\node.exe" (SET "_prog=%dp0%\node.exe") ELSE (SET "_prog=node")
	endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@openai\codex\bin\codex.js" %*
	""",
)
SHIM_PATH = "C:\\Users\\Jo\\AppData\\Roaming\\npm\\codex.cmd"
SCRIPT_PATH = "C:\\Users\\Jo\\AppData\\Roaming\\npm\\node_modules\\@openai\\codex\\bin\\codex.js"


class TestResolveCmdShim(unittest.TestCase):
	def resolve(self, existing, which=lambda name: None, content=SHIM, env=WIN_ENV):
		return codexClient.resolveCmdShim(
			SHIM_PATH,
			readText=lambda p: content,
			which=which,
			isFile=lambda p: p in existing,
			env=env,
		)

	def test_nodeNextToShim(self):
		local = "C:\\Users\\Jo\\AppData\\Roaming\\npm\\node.exe"
		self.assertEqual(self.resolve({SCRIPT_PATH, local}), [local, SCRIPT_PATH])

	def test_nodeOnPath(self):
		self.assertEqual(
			self.resolve({SCRIPT_PATH}, which=lambda name: "C:\\Tools\\node.exe"),
			["C:\\Tools\\node.exe", SCRIPT_PATH],
		)

	def test_nodeInProgramFiles(self):
		# C4: NVDA may have started before Node.js was installed, so PATH can be stale.
		programFilesNode = "C:\\Program Files\\nodejs\\node.exe"
		self.assertEqual(self.resolve({SCRIPT_PATH, programFilesNode}), [programFilesNode, SCRIPT_PATH])

	def test_nodeMissingIsReported(self):
		# C4: script found but no Node.js -> a Node.js-specific notInstalled error, not a cmd.exe attempt.
		with self.assertRaises(CodexError) as cm:
			self.resolve({SCRIPT_PATH})
		self.assertEqual(cm.exception.kind, codexClient.KIND_NOT_INSTALLED)
		self.assertEqual(cm.exception.reason, codexClient.REASON_NODE_MISSING)
		self.assertIn("Node.js", cm.exception.message)

	def test_missingScriptIsReported(self):
		with self.assertRaises(CodexError) as cm:
			self.resolve(set(), which=lambda name: "node.exe")
		self.assertEqual(cm.exception.reason, codexClient.REASON_INCOMPLETE_INSTALL)

	def test_unrecognisedShimFallsBackToCmd(self):
		self.assertIsNone(self.resolve({SCRIPT_PATH}, content="@echo off\nrun-something.exe %*"))
		command = codexClient.buildCommand(
			SHIM_PATH,
			["exec"],
			isWindows=True,
			resolveShim=lambda path: self.resolve({SCRIPT_PATH}, content="@echo off"),
			env=WIN_ENV,
		)
		self.assertTrue(command.startswith('"C:\\Windows\\System32\\cmd.exe" /d /v:off /s /c'))

	def test_unreadableShimFallsBackToCmd(self):
		def unreadable(path):
			raise OSError("denied")

		self.assertIsNone(codexClient.resolveCmdShim(SHIM_PATH, readText=unreadable))


class TestFindCodex(unittest.TestCase):
	def test_userPathWins(self):
		self.assertEqual(
			codexClient.findCodex('"C:\\x\\codex.exe"', which=lambda n: "other", isFile=lambda p: True),
			"C:\\x\\codex.exe",
		)
		self.assertIsNone(codexClient.findCodex("C:\\missing.exe", which=lambda n: "other", isFile=lambda p: False))

	def test_userPathEnvironmentVariablesAreExpanded(self):
		# A8: paths such as %APPDATA%\npm\codex.cmd work (os.path.expandvars; $VAR on POSIX, %VAR% on Windows).
		with mock.patch.dict(os.environ, {"ASKGPT_TEST_DIR": "/opt/tools"}):
			variable = "%ASKGPT_TEST_DIR%" if os.name == "nt" else "$ASKGPT_TEST_DIR"
			found = codexClient.findCodex(variable + "/codex", which=lambda n: None, isFile=lambda p: p == "/opt/tools/codex")
			self.assertEqual(found, "/opt/tools/codex")
			self.assertEqual(codexClient.expandUserPath(' "%s/codex" ' % variable), "/opt/tools/codex")

	def test_pathLookup(self):
		self.assertEqual(
			codexClient.findCodex("", which=lambda n: "/usr/bin/codex", isFile=lambda p: False, isWindows=False),
			"/usr/bin/codex",
		)

	def test_windowsExtensionlessShimPrefersCmd(self):
		found = codexClient.findCodex(
			"",
			env={},
			which=lambda n: "C:\\npm\\codex",
			isFile=lambda p: p == "C:\\npm\\codex.cmd",
			isWindows=True,
		)
		self.assertEqual(found, "C:\\npm\\codex.cmd")

	def test_windowsWellKnownLocations(self):
		env = {"APPDATA": "C:\\AD", "LOCALAPPDATA": "C:\\LAD", "USERPROFILE": "C:\\UP"}
		candidates = codexClient.candidatePaths(env, isWindows=True)
		self.assertEqual(candidates[0], "C:\\AD\\npm\\codex.cmd")
		self.assertIn("C:\\UP\\.local\\bin\\codex.exe", candidates)
		self.assertTrue(any(c.startswith("C:\\LAD\\Programs") for c in candidates))
		target = "C:\\UP\\.local\\bin\\codex.exe"
		found = codexClient.findCodex("", env=env, which=lambda n: None, isFile=lambda p: p == target, isWindows=True)
		self.assertEqual(found, target)
		self.assertIsNone(codexClient.findCodex("", env=env, which=lambda n: None, isFile=lambda p: False, isWindows=True))


class TestWorkDir(TempDirTestCase):
	def test_defaultWorkDir(self):
		# C6: outside NVDA's configuration folder.
		self.assertEqual(
			codexClient.defaultWorkDir({"LOCALAPPDATA": "/lad"}, isWindows=True),
			os.path.join("/lad", "askGPT", "workdir"),
		)
		self.assertEqual(
			codexClient.defaultWorkDir({}, isWindows=True),
			os.path.join(tempfile.gettempdir(), "askGPT-workdir"),
		)
		self.assertEqual(
			codexClient.defaultWorkDir({"LOCALAPPDATA": "/lad"}, isWindows=False),
			os.path.join(tempfile.gettempdir(), "askGPT-workdir"),
		)

	def test_cleanStaleAnswerFiles(self):
		for name in ("answer-abc.txt", "answer-def.txt", "notes.txt", "answer-x.log"):
			with open(os.path.join(self.workDir, name), "w") as f:
				f.write("x")
		self.assertEqual(codexClient.cleanStaleAnswerFiles(self.workDir), 2)
		self.assertEqual(sorted(os.listdir(self.workDir)), ["answer-x.log", "notes.txt"])
		self.assertEqual(codexClient.cleanStaleAnswerFiles(os.path.join(self.workDir, "missing")), 0)


# A realistic `codex exec` stderr: header, echoed prompt, reasoning summary, model text, then an error.
REALISTIC_STDERR = """OpenAI Codex v0.160.0
--------
workdir: C:\\Users\\Jo\\AppData\\Local\\askGPT\\workdir
model: gpt-5
provider: openai
approval: never
sandbox: read-only
reasoning effort: low
reasoning summaries: auto
session id: 0199a429-7f3e-7d21-9401-4290a4c1b401
--------
user
{prompt}
thinking
**Explaining design in Python**
I should explain how to log in to the dialog in the app; signing is unrelated. Status 429 is not ours.
codex
You can sign in from the dialog in the settings. A rate limit of 401 requests applies.
{error}
"""


class TestClassifyFailure(unittest.TestCase):
	def assertKind(self, kind, stderr, stdout="", prompt="", returnCode=1):
		error = codexClient.classifyFailure(returnCode, stdout, stderr, prompt)
		self.assertEqual(error.kind, kind, stderr)
		return error

	def realistic(self, errorLines, question="How do I log in? It said 401 unauthorized and rate limit 429."):
		prompt = codexClient.buildPrompt(question)
		return REALISTIC_STDERR.format(prompt=prompt.strip(), error=errorLines), prompt

	def test_reportedFalseUsageLimit(self):
		# C2: the coordinator's repro, previously classified as usageLimit.
		error = self.assertKind(
			codexClient.KIND_NETWORK,
			"session id: 0199a429-...\n**Explaining design in Python**\nERROR: stream disconnected",
		)
		self.assertEqual(error.message, "Couldn't reach ChatGPT. Check your internet connection and try again.")

	def test_modelTextAndHeaderAreIgnored(self):
		stderr, prompt = self.realistic("ERROR: something unexpected happened")
		error = self.assertKind(codexClient.KIND_OTHER, stderr, prompt=prompt)
		self.assertIn("something unexpected happened", error.detail)
		self.assertNotIn("design", error.detail)
		self.assertNotIn("session id", error.detail)

	def test_realisticNetworkFailures(self):
		for line in (
			"ERROR: stream disconnected before completion: error sending request for url (https://chatgpt.com/backend-api/codex/responses)",
			"2026-10-31T01:13:54.671548Z ERROR codex_core: error sending request: dns error: failed to lookup address information",
			"ERROR: connection reset by peer",
			"ERROR: invalid peer certificate: UnknownIssuer",
		):
			stderr, prompt = self.realistic(line)
			self.assertKind(codexClient.KIND_NETWORK, stderr, prompt=prompt)

	def test_realisticNotLoggedIn(self):
		# Real output of codex-cli 0.160.0 with no credentials.
		stderr, prompt = self.realistic(
			"2026-10-31T01:13:56.374839Z ERROR codex_api::endpoint::responses_websocket: failed to connect to "
			"websocket: HTTP error: 401 Unauthorized, url: wss://api.openai.com/v1/responses\n"
			"ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication in header",
		)
		self.assertKind(codexClient.KIND_NOT_LOGGED_IN, stderr, prompt=prompt)
		self.assertKind(codexClient.KIND_NOT_LOGGED_IN, "Not logged in")
		self.assertKind(codexClient.KIND_NOT_LOGGED_IN, "Error: please log in again with codex login")
		self.assertKind(codexClient.KIND_NOT_LOGGED_IN, "ERROR: Your access token has expired")

	def test_realisticUsageLimit(self):
		for line in (
			"ERROR: You've hit your usage limit. Upgrade to Pro or try again at 3:05 PM.",
			"ERROR: unexpected status 429 Too Many Requests",
			"ERROR: exceeded retry limit, last status: 429",
		):
			stderr, prompt = self.realistic(line)
			self.assertKind(codexClient.KIND_USAGE_LIMIT, stderr, prompt=prompt)

	def test_wordBoundaries(self):
		for text in (
			"ERROR: failed while designing in the editor",
			"ERROR: the dialog in front failed",
			"ERROR: signing failed",
			"ERROR: catalogin error",
		):
			self.assertKind(codexClient.KIND_OTHER, text)

	def test_numbersNeedContext(self):
		self.assertKind(codexClient.KIND_OTHER, "ERROR: stream ended\ntokens used\n28,401")
		self.assertKind(codexClient.KIND_OTHER, "ERROR: item 429 failed to parse")
		self.assertKind(codexClient.KIND_OTHER, "ERROR: request 401 of 500 failed")

	def test_notInstalled(self):
		self.assertKind(
			codexClient.KIND_NOT_INSTALLED,
			"'node' is not recognized as an internal or external command,\noperable program or batch file.",
		)

	def test_otherDetailIsTechnicalAndMessageIsNot(self):
		stderr = "x" * 1000 + "\nERROR: model gpt-9 is not supported"
		error = self.assertKind(codexClient.KIND_OTHER, stderr)
		self.assertEqual(error.message, "Codex could not answer (exit code 1).")
		self.assertTrue(error.detail.endswith("model gpt-9 is not supported"))
		self.assertLessEqual(len(error.detail), 300)

	def test_diagnosticLines(self):
		stderr, prompt = self.realistic("ERROR: boom\nwarning: trailing note")
		errorLines, tail = codexClient.diagnosticLines(stderr, prompt)
		self.assertEqual(errorLines, ["ERROR: boom"])
		self.assertEqual(tail, ["warning: trailing note"])


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
		self.grandchildPidFile = os.path.join(self.workDir, "grandchild.pid")
		with open(self.exe, "w", encoding="utf-8") as f:
			f.write(
				"#!%s\n" % sys.executable
				+ textwrap.dedent(
					"""
					import subprocess, sys, time
					args = sys.argv[1:]
					if args[:1] == ["--version"]:
						print("codex-cli 9.9.9"); sys.exit(0)
					prompt = sys.stdin.buffer.read().decode("utf-8")
					if "SLEEP" in prompt:
						child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
						with open(%r, "w") as f:
							f.write(str(child.pid))
						time.sleep(60)
					sys.stderr.write("user\\n" + prompt + "\\n")
					out = args[args.index("-o") + 1]
					with open(out, "w", encoding="utf-8") as f:
						f.write("ECHO:" + prompt.strip().splitlines()[-1])
					""" % self.grandchildPidFile,
				),
			)
		os.chmod(self.exe, os.stat(self.exe).st_mode | stat.S_IEXEC)

	def test_roundTrip(self):
		client = CodexClient(executablePath=self.exe, workDir=self.workDir, isWindows=False)
		self.assertEqual(client.version(), "codex-cli 9.9.9")
		answer = client.ask(codexClient.buildPrompt("caf\u00e9 \"quotes\" & pipes |"))
		self.assertEqual(answer, "ECHO:Question: caf\u00e9 \"quotes\" & pipes |")
		self.assertEqual([n for n in os.listdir(self.workDir) if n.startswith("answer-")], [])

	def test_timeoutKillsWholeProcessGroup(self):
		# C10: the grandchild must die too, not just codex.
		client = CodexClient(executablePath=self.exe, workDir=self.workDir, isWindows=False)
		client.timeout = 1.5
		with self.assertRaises(CodexError) as cm:
			client.ask("SLEEP please")
		self.assertEqual(cm.exception.kind, codexClient.KIND_TIMEOUT)
		with open(self.grandchildPidFile) as f:
			grandchild = int(f.read())
		deadline = time.monotonic() + 5
		alive = True
		while alive and time.monotonic() < deadline:
			try:
				with open("/proc/%d/stat" % grandchild) as f:
					alive = f.read().split()[2] != "Z"
			except FileNotFoundError:
				alive = False
			except OSError:
				try:
					os.kill(grandchild, 0)
				except ProcessLookupError:
					alive = False
			if alive:
				time.sleep(0.05)
		if alive:
			os.kill(grandchild, 9)
		self.assertFalse(alive, "grandchild survived the timeout kill")


if __name__ == "__main__":
	unittest.main()
