"""Tests for the Codex provider and the shared process runner in providers.base (no NVDA, no real Codex needed)."""

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

from providers import base, codex
from providers.codex import CodexClient, CodexError

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
		prompt = base.buildPrompt(NASTY_QUESTION)
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
		self.assertEqual(cm.exception.kind, base.KIND_CONFIG)
		self.assertEqual(popen.calls, [])
		# Effort names come from Codex's model list (xhigh, max ...), but must stay command-line safe.
		for effort in ("x high", "low;calc", 'low"', "LOW&", "-c"):
			with self.assertRaises(CodexError, msg=effort):
				codex.buildExecArgs("out.txt", reasoningEffort=effort)
		self.assertIn("model_reasoning_effort=xhigh", codex.buildExecArgs("out.txt", reasoningEffort="xhigh"))

	def test_windowsCreationFlagsAndJob(self):
		FakeJob.instances.clear()
		popen = FakePopen(outputText="ok")
		client = makeClient(popen, self.workDir, isWindows=True)
		client.ask("hi")
		kwargs = popen.calls[-1][1]
		self.assertEqual(kwargs["creationflags"], base.CREATE_NO_WINDOW)
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
		self.assertEqual(cm.exception.kind, base.KIND_OTHER)
		self.assertEqual(cm.exception.reason, base.REASON_EMPTY_ANSWER)

	def test_emptyAnswerWithExitZeroIsClassified(self):
		# C7: Codex can exit 0 after an error; the error must be reported, not "empty answer".
		popen = FakePopen(returncode=0, outputText="", stderr=b"ERROR: unexpected status 401 Unauthorized")
		with self.assertRaises(CodexError) as cm:
			makeClient(popen, self.workDir).ask("hi")
		self.assertEqual(cm.exception.kind, base.KIND_NOT_LOGGED_IN)

	def test_failureIsClassified(self):
		popen = FakePopen(returncode=1, stderr=b"ERROR: unexpected status 401 Unauthorized: Missing bearer")
		with self.assertRaises(CodexError) as cm:
			makeClient(popen, self.workDir).ask("hi")
		self.assertEqual(cm.exception.kind, base.KIND_NOT_LOGGED_IN)

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

		with mock.patch.object(base.os, "remove", flakyRemove), mock.patch.object(base.time, "sleep"):
			makeClient(popen, self.workDir).ask("hi")
		self.assertEqual(len(calls), 2)
		self.assertEqual(os.listdir(self.workDir), [])


class TestProjectMode(TempDirTestCase):
	"""Feature 2: with a project folder Codex works there (-C) and, if allowed, may write files."""

	def setUp(self):
		super().setUp()
		self.project = os.path.join(self.workDir, "My Project & Notes")
		os.makedirs(self.project)

	def test_withoutProjectStaysReadOnlyInPrivateFolder(self):
		args = codex.buildExecArgs("out.txt")
		self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
		self.assertNotIn("-C", args)
		self.assertFalse(any("sandbox_workspace_write" in a for a in args))

	def test_projectWithWritesUsesWorkspaceWriteWithoutNetwork(self):
		args = codex.buildExecArgs("out.txt", projectFolder=self.project, allowWrites=True, isWindows=False)
		self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")
		self.assertEqual(args[args.index("-C") + 1], self.project)
		self.assertIn("--skip-git-repo-check", args)
		for override in (
			"sandbox_workspace_write.network_access=false",
			"sandbox_workspace_write.exclude_tmpdir_env_var=true",
			"sandbox_workspace_write.exclude_slash_tmp=true",
		):
			self.assertEqual(args[args.index(override) - 1], "-c")
		self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
		self.assertNotIn("danger-full-access", args)
		self.assertEqual(args[-1], "-")

	def test_projectWithoutWritesStaysReadOnly(self):
		args = codex.buildExecArgs("out.txt", projectFolder=self.project, allowWrites=False, isWindows=False)
		self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
		self.assertEqual(args[args.index("-C") + 1], self.project, "Codex can still read the project")
		self.assertFalse(any("sandbox_workspace_write" in a for a in args))

	def test_folderArgumentNeverEndsInBackslash(self):
		self.assertEqual(base.projectFolderArgument("C:\\", isWindows=True), "C:\\.")
		self.assertEqual(base.projectFolderArgument("D:\\Work\\Book\\", isWindows=True), "D:\\Work\\Book")
		self.assertEqual(base.projectFolderArgument("/", isWindows=False), "/.")
		# The cmd.exe fallback accepts project paths with & and quotes them.
		args = codex.buildExecArgs("C:\\w\\out.txt", projectFolder="C:\\", allowWrites=True, isWindows=True)
		command = base.buildCommand("C:\\x\\codex.cmd", args, isWindows=True, resolveShim=lambda p: None, env=WIN_ENV)
		self.assertIn('"-C" "C:\\."', command)
		args = codex.buildExecArgs("C:\\w\\out.txt", projectFolder="C:\\R&D Docs", allowWrites=True, isWindows=True)
		command = base.buildCommand("C:\\x\\codex.cmd", args, isWindows=True, resolveShim=lambda p: None, env=WIN_ENV)
		self.assertIn('"-C" "C:\\R&D Docs"', command)
		with self.assertRaises(CodexError):
			args = codex.buildExecArgs("out.txt", projectFolder="C:\\100%", allowWrites=True, isWindows=True)
			base.buildCommand("C:\\x\\codex.cmd", args, isWindows=True, resolveShim=lambda p: None, env=WIN_ENV)

	def test_askRunsInTheProjectFolder(self):
		popen = FakePopen(outputText="Saved notes.txt")
		client = makeClient(popen, self.workDir, projectFolder=self.project, allowProjectWrites=True)
		self.assertEqual(client.ask("Question: write notes"), "Saved notes.txt")
		argv = popen.lastArgv
		kwargs = popen.calls[-1][1]
		self.assertEqual(kwargs["cwd"], self.project)
		self.assertEqual(argv[argv.index("-C") + 1], self.project)
		self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
		outputFile = argv[argv.index("-o") + 1]
		self.assertEqual(os.path.dirname(outputFile), self.workDir, "the answer file never goes into the project")
		self.assertEqual(os.listdir(self.project), [])

	def test_missingProjectFolderIsReported(self):
		popen = FakePopen(outputText="x")
		client = makeClient(popen, self.workDir, projectFolder=os.path.join(self.workDir, "gone"))
		with self.assertRaises(CodexError) as cm:
			client.ask("q")
		self.assertEqual(cm.exception.kind, base.KIND_CONFIG)
		self.assertEqual(cm.exception.reason, base.REASON_PROJECT_MISSING)
		self.assertEqual(popen.calls, [])

	def test_projectPromptInstructions(self):
		plain = base.buildPrompt("q")
		self.assertNotIn("project folder", plain)
		project = base.buildPrompt("q", projectInstructions=codex.CodexProvider.projectInstructions(True))
		self.assertIn(codex.PROJECT_INSTRUCTIONS, project)
		readOnly = base.buildPrompt("q", projectInstructions=codex.CodexProvider.projectInstructions(False))
		self.assertIn("no tools to open or change files", readOnly)
		self.assertIn("tell the user the exact file name", project)
		self.assertIn("Do not modify or delete existing files unless the user asks.", project)
		self.assertTrue(project.startswith(base.DEFAULT_INSTRUCTIONS))

	def test_modelSlugsWithDots(self):
		self.assertTrue(base.isValidModel("gpt-6.1-sol"))
		args = codex.buildExecArgs("out.txt", model="gpt-6.1-sol", reasoningEffort="xhigh")
		self.assertEqual(args[args.index("-m") + 1], "gpt-6.1-sol")


class TestHardening(unittest.TestCase):
	"""S-H2: flags verified with codex-cli 0.160.0 (`codex exec --help`, `codex features list`)."""

	def test_alwaysPresent(self):
		for folder, writes in (("", False), ("", True), ("/p", False), ("/p", True)):
			args = codex.buildExecArgs("/w/a.txt", projectFolder=folder, allowWrites=writes, isWindows=False)
			for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral"):
				self.assertIn(flag, args)
			disabled = [args[i + 1] for i, arg in enumerate(args) if arg == "--disable"]
			for feature in ("apps", "plugins", "browser_use", "computer_use", "multi_agent", "image_generation", "hooks"):
				self.assertIn(feature, disabled)
			self.assertEqual(args[args.index("web_search=disabled") - 1], "-c")
			self.assertEqual(args[-1], "-")
			for forbidden in ("--dangerously", "danger-full-access", "--search", "--enable"):
				self.assertNotIn(forbidden, " ".join(args))

	def test_shellOnlyInWritableProject(self):
		def disabled(folder, writes):
			args = codex.buildExecArgs("/w/a.txt", projectFolder=folder, allowWrites=writes, isWindows=False)
			return [args[i + 1] for i, arg in enumerate(args) if arg == "--disable"]

		for folder, writes in (("", False), ("", True), ("/p", False)):
			self.assertIn("shell_tool", disabled(folder, writes))
			self.assertIn("unified_exec", disabled(folder, writes))
		self.assertNotIn("shell_tool", disabled("/p", True))
		self.assertFalse(codex.CodexProvider.readsOtherFiles)


class TestProcessControl(TempDirTestCase):
	def test_timeoutKillsProcessGroup(self):
		popen = FakePopen(hang=True)
		client = makeClient(popen, self.workDir)
		client.timeout = 0.3  # Below the user-facing minimum, for a fast test.
		with self.assertRaises(CodexError) as cm:
			client.ask("hi")
		self.assertEqual(cm.exception.kind, base.KIND_TIMEOUT)
		self.assertTrue(popen.lastProcess.killed)
		client._killpg.assert_called_once_with(4242, base.SIGKILL)

	def test_cancelKillsProcess(self):
		popen = FakePopen(hang=True)
		client = makeClient(popen, self.workDir)
		cancelEvent = threading.Event()
		timer = threading.Timer(0.2, cancelEvent.set)
		timer.start()
		self.addCleanup(timer.cancel)
		with self.assertRaises(CodexError) as cm:
			client.ask("hi", cancelEvent=cancelEvent)
		self.assertEqual(cm.exception.kind, base.KIND_CANCELLED)
		self.assertTrue(popen.lastProcess.killed)

	def test_windowsTimeoutUsesJob(self):
		# C5: the Job Object kills the whole tree; taskkill is not needed.
		FakeJob.instances.clear()
		popen = FakePopen(hang=True)
		client = makeClient(popen, self.workDir, isWindows=True)
		client.timeout = 0.3
		with mock.patch.object(base.subprocess, "run") as run:
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
		with mock.patch.object(base.subprocess, "run") as run:
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
		# Fix round 3, item 3: a kill by terminate() is a cancellation, not "Codex could not answer (exit code 1)".
		self.assertEqual(errors[0].kind, base.KIND_CANCELLED)

	def test_terminateDuringPosixRunIsCancelled(self):
		popen = FakePopen(hang=True)
		client = makeClient(popen, self.workDir)
		errors = []
		thread = threading.Thread(target=lambda: errors.append(self._askError(client)))
		thread.start()
		for _ in range(100):
			if popen.processes:
				break
			time.sleep(0.01)
		client.terminate()
		thread.join(5)
		self.assertEqual(errors[0].kind, base.KIND_CANCELLED)

	@staticmethod
	def _askError(client, cancelEvent=None):
		try:
			client.ask("hi", cancelEvent)
		except CodexError as e:
			return e
		return None

	def test_cancelSetWhileProcessExitsIsCancelled(self):
		# The process exits with code 1 (killed) and the loop sees the exit before the cancel flag.
		popen = FakePopen(returncode=1, stderr=b"ERROR: interrupted")
		cancelEvent = threading.Event()
		cancelEvent.set()
		error = self._askError(makeClient(popen, self.workDir), cancelEvent)
		self.assertEqual(error.kind, base.KIND_CANCELLED)

	def test_ordinaryFailureIsNotReportedAsCancelled(self):
		popen = FakePopen(returncode=1, stderr=b"ERROR: interrupted")
		error = self._askError(makeClient(popen, self.workDir), threading.Event())
		self.assertEqual(error.kind, base.KIND_OTHER)
		# terminate() with nothing running must not mark the next run as killed.
		client = makeClient(FakePopen(returncode=1, stderr=b"ERROR: interrupted"), self.workDir)
		client.terminate()
		self.assertEqual(self._askError(client).kind, base.KIND_OTHER)

	def test_windowsKillUsesAbsoluteTaskkill(self):
		proc = helpers.FakeProcess(["x"], hang=True)
		run = mock.Mock()
		base.killProcessTree(proc, isWindows=True, run=run, env=WIN_ENV)
		argv = run.call_args[0][0]
		self.assertEqual(argv, ["C:\\Windows\\System32\\taskkill.exe", "/T", "/F", "/PID", "4242"])
		self.assertTrue(proc.killed)

	def test_killSkipsFinishedProcess(self):
		proc = helpers.FakeProcess(["x"], returncode=0)
		run = mock.Mock()
		base.killProcessTree(proc, isWindows=True, run=run)
		run.assert_not_called()
		self.assertFalse(proc.killed)

	def test_missingExecutable(self):
		client = CodexClient(workDir=self.workDir, popen=FakePopen(), finder=lambda userPath: None)
		with self.assertRaises(CodexError) as cm:
			client.ask("hi")
		self.assertEqual(cm.exception.kind, base.KIND_NOT_INSTALLED)
		self.assertEqual(cm.exception.reason, "")

	def test_missingCustomPath(self):
		# A4: a wrong configured path gets its own message, naming the path.
		client = CodexClient(executablePath="C:\\nope\\codex.exe", workDir=self.workDir, finder=lambda userPath: None)
		with self.assertRaises(CodexError) as cm:
			client.locate()
		self.assertEqual(cm.exception.reason, base.REASON_CUSTOM_PATH)
		self.assertEqual(cm.exception.detail, "C:\\nope\\codex.exe")

	def test_popenFileNotFound(self):
		def popen(*args, **kwargs):
			raise FileNotFoundError("nope")

		client = makeClient(popen, self.workDir)
		with self.assertRaises(CodexError) as cm:
			client.ask("hi")
		self.assertEqual(cm.exception.kind, base.KIND_NOT_INSTALLED)

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
		self.assertTrue(kwargs["creationflags"] & base.CREATE_NO_WINDOW)
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
		self.assertEqual(client.loginStatus(), (base.STATE_SUBSCRIPTION, "Logged in using ChatGPT"))
		self.assertEqual(popen.lastArgv, [FAKE_EXE, "login", "status"])
		self.assertEqual(client.logout()[0], True)
		self.assertEqual(popen.lastArgv, [FAKE_EXE, "logout"])
		popen = FakePopen(returncode=0, stdout=b"codex-cli 0.160.0\n")
		self.assertEqual(makeClient(popen, self.workDir).version(), "codex-cli 0.160.0")
		self.assertEqual(popen.lastArgv, [FAKE_EXE, "--version"])

	def test_extractUrl(self):
		self.assertEqual(base.extractUrl("go to https://a.example/x?y=1."), "https://a.example/x?y=1")
		self.assertIsNone(base.extractUrl("server on http://localhost:1455"))


class TestJobObjectDeclarations(unittest.TestCase):
	"""C5: the ctypes structures must match the Windows layout for the running pointer size."""

	def test_structureLayout(self):
		ext = base.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
		basic = base.JOBOBJECT_BASIC_LIMIT_INFORMATION
		self.assertEqual(ctypes.sizeof(base.IO_COUNTERS), 48)
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
		job = base.WindowsJob(kernel32)
		args = kernel32.SetInformationJobObject.call_args[0]
		self.assertEqual(args[0], 1234)
		self.assertEqual(args[1], 9)
		info = args[2]._obj
		self.assertEqual(info.BasicLimitInformation.LimitFlags, 0x2000)
		self.assertEqual(args[3], ctypes.sizeof(base.JOBOBJECT_EXTENDED_LIMIT_INFORMATION))
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
			base.WindowsJob(kernel32)
		kernel32.CloseHandle.assert_called_once_with(99)

	def test_kernel32PrototypesUseHandles(self):
		# Re-confirmed for 64-bit NVDA 2026.x: every handle argument and return value is HANDLE (c_void_p),
		# never a 32-bit int, and the structure goes by pointer.
		from ctypes import wintypes

		fakeDll = mock.MagicMock()
		with mock.patch.object(ctypes, "WinDLL", create=True, return_value=fakeDll) as winDll, mock.patch.object(
			base,
			"_kernel32",
			None,
		):
			kernel32 = base._loadKernel32()
		winDll.assert_called_once_with("kernel32", use_last_error=True)
		self.assertIs(kernel32, fakeDll)
		HANDLE = wintypes.HANDLE
		self.assertIs(HANDLE, ctypes.c_void_p)
		extPointer = ctypes.POINTER(base.JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
		expected = {
			"CreateJobObjectW": ((ctypes.c_void_p, wintypes.LPCWSTR), HANDLE),
			"SetInformationJobObject": ((HANDLE, ctypes.c_int, extPointer, wintypes.DWORD), wintypes.BOOL),
			"AssignProcessToJobObject": ((HANDLE, HANDLE), wintypes.BOOL),
			"TerminateJobObject": ((HANDLE, wintypes.UINT), wintypes.BOOL),
			"CloseHandle": ((HANDLE,), wintypes.BOOL),
		}
		for name, (argtypes, restype) in expected.items():
			function = getattr(fakeDll, name)
			self.assertEqual(tuple(function.argtypes), argtypes, name)
			self.assertIs(function.restype, restype, name)

	def test_moduleImportsWithoutWindowsDlls(self):
		# Loading kernel32 is deferred, so importing on Linux works and the default job factory is lazy.
		self.assertIsNone(base._kernel32)


class TestCommandBuilding(unittest.TestCase):
	def test_cmdFallbackQuotesEveryArgument(self):
		# C1: & | < > ^ in paths must be inside quotes; cmd.exe is called by absolute path with /v:off.
		outputFile = "C:\\Users\\R&D <Team>\\AppData\\Local\\askAI\\workdir\\answer-1.txt"
		args = codex.buildExecArgs(outputFile, reasoningEffort="low")
		command = base.buildCommand(
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
				base.buildCommand(bad, ["exec"], isWindows=True, resolveShim=lambda p: None, env=WIN_ENV)
		with self.assertRaises(CodexError):
			base.quoteForCmd(["C:\\dir\\"])

	def test_resolvedShimRunsNodeDirectly(self):
		command = base.buildCommand(
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
			base.buildCommand("C:\\bin\\codex.exe", ["exec"], isWindows=True),
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
		return base.resolveCmdShim(
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
		self.assertEqual(cm.exception.kind, base.KIND_NOT_INSTALLED)
		self.assertEqual(cm.exception.reason, base.REASON_NODE_MISSING)
		self.assertIn("Node.js", cm.exception.message)

	def test_missingScriptIsReported(self):
		with self.assertRaises(CodexError) as cm:
			self.resolve(set(), which=lambda name: "node.exe")
		self.assertEqual(cm.exception.reason, base.REASON_INCOMPLETE_INSTALL)

	def test_unrecognisedShimFallsBackToCmd(self):
		self.assertIsNone(self.resolve({SCRIPT_PATH}, content="@echo off\nrun-something.exe %*"))
		command = base.buildCommand(
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

		self.assertIsNone(base.resolveCmdShim(SHIM_PATH, readText=unreadable))


class TestFindCodex(unittest.TestCase):
	def test_userPathWins(self):
		self.assertEqual(
			codex.findCodex('"C:\\x\\codex.exe"', which=lambda n: "other", isFile=lambda p: True),
			"C:\\x\\codex.exe",
		)
		self.assertIsNone(codex.findCodex("C:\\missing.exe", which=lambda n: "other", isFile=lambda p: False))

	def test_userPathEnvironmentVariablesAreExpanded(self):
		# A8: paths such as %APPDATA%\npm\codex.cmd work (os.path.expandvars; $VAR on POSIX, %VAR% on Windows).
		with mock.patch.dict(os.environ, {"ASKGPT_TEST_DIR": "/opt/tools"}):
			variable = "%ASKGPT_TEST_DIR%" if os.name == "nt" else "$ASKGPT_TEST_DIR"
			found = codex.findCodex(variable + "/codex", which=lambda n: None, isFile=lambda p: p == "/opt/tools/codex")
			self.assertEqual(found, "/opt/tools/codex")
			self.assertEqual(base.expandUserPath(' "%s/codex" ' % variable), "/opt/tools/codex")

	def test_pathLookup(self):
		self.assertEqual(
			codex.findCodex("", which=lambda n: "/usr/bin/codex", isFile=lambda p: False, isWindows=False),
			"/usr/bin/codex",
		)

	def test_windowsExtensionlessShimPrefersCmd(self):
		found = codex.findCodex(
			"",
			env={},
			which=lambda n: "C:\\npm\\codex",
			isFile=lambda p: p == "C:\\npm\\codex.cmd",
			isWindows=True,
		)
		self.assertEqual(found, "C:\\npm\\codex.cmd")

	def test_windowsWellKnownLocations(self):
		env = {"APPDATA": "C:\\AD", "LOCALAPPDATA": "C:\\LAD", "USERPROFILE": "C:\\UP"}
		candidates = codex.candidatePaths(env, isWindows=True)
		self.assertEqual(candidates[0], "C:\\AD\\npm\\codex.cmd")
		self.assertIn("C:\\UP\\.local\\bin\\codex.exe", candidates)
		self.assertTrue(any(c.startswith("C:\\LAD\\Programs") for c in candidates))
		target = "C:\\UP\\.local\\bin\\codex.exe"
		found = codex.findCodex("", env=env, which=lambda n: None, isFile=lambda p: p == target, isWindows=True)
		self.assertEqual(found, target)
		self.assertIsNone(codex.findCodex("", env=env, which=lambda n: None, isFile=lambda p: False, isWindows=True))


class TestWorkDir(TempDirTestCase):
	def test_defaultWorkDir(self):
		# C6: outside NVDA's configuration folder.
		self.assertEqual(
			base.defaultWorkDir({"LOCALAPPDATA": "/lad"}, isWindows=True),
			os.path.join("/lad", "askAI", "workdir"),
		)
		self.assertEqual(
			base.defaultWorkDir({}, isWindows=True),
			os.path.join(tempfile.gettempdir(), "askAI-workdir"),
		)
		self.assertEqual(
			base.defaultWorkDir({"LOCALAPPDATA": "/lad"}, isWindows=False),
			os.path.join(tempfile.gettempdir(), "askAI-workdir"),
		)

	def test_cleanStaleAnswerFiles(self):
		for name in ("answer-abc.txt", "answer-def.txt", "notes.txt", "answer-x.log"):
			with open(os.path.join(self.workDir, name), "w") as f:
				f.write("x")
		self.assertEqual(base.cleanStaleAnswerFiles(self.workDir), 2)
		self.assertEqual(sorted(os.listdir(self.workDir)), ["answer-x.log", "notes.txt"])
		self.assertEqual(base.cleanStaleAnswerFiles(os.path.join(self.workDir, "missing")), 0)


# A realistic `codex exec` stderr: header, echoed prompt, reasoning summary, model text, then an error.
REALISTIC_STDERR = """OpenAI Codex v0.160.0
--------
workdir: C:\\Users\\Jo\\AppData\\Local\\askAI\\workdir
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
		error = codex.classifyFailure(returnCode, stdout, stderr, prompt)
		self.assertEqual(error.kind, kind, stderr)
		return error

	def realistic(self, errorLines, question="How do I log in? It said 401 unauthorized and rate limit 429."):
		prompt = base.buildPrompt(question)
		return REALISTIC_STDERR.format(prompt=prompt.strip(), error=errorLines), prompt

	def test_reportedFalseUsageLimit(self):
		# C2: the coordinator's repro, previously classified as usageLimit.
		error = self.assertKind(
			base.KIND_NETWORK,
			"session id: 0199a429-...\n**Explaining design in Python**\nERROR: stream disconnected",
		)
		self.assertEqual(error.message, "Couldn't reach ChatGPT. Check your internet connection and try again.")

	def test_modelTextAndHeaderAreIgnored(self):
		stderr, prompt = self.realistic("ERROR: something unexpected happened")
		error = self.assertKind(base.KIND_OTHER, stderr, prompt=prompt)
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
			self.assertKind(base.KIND_NETWORK, stderr, prompt=prompt)

	def test_realisticNotLoggedIn(self):
		# Real output of codex-cli 0.160.0 with no credentials.
		stderr, prompt = self.realistic(
			"2026-10-31T01:13:56.374839Z ERROR codex_api::endpoint::responses_websocket: failed to connect to "
			"websocket: HTTP error: 401 Unauthorized, url: wss://api.openai.com/v1/responses\n"
			"ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication in header",
		)
		self.assertKind(base.KIND_NOT_LOGGED_IN, stderr, prompt=prompt)
		self.assertKind(base.KIND_NOT_LOGGED_IN, "Not logged in")
		self.assertKind(base.KIND_NOT_LOGGED_IN, "Error: please log in again with codex login")
		self.assertKind(base.KIND_NOT_LOGGED_IN, "ERROR: Your access token has expired")

	def test_realisticUsageLimit(self):
		for line in (
			"ERROR: You've hit your usage limit. Upgrade to Pro or try again at 3:05 PM.",
			"ERROR: unexpected status 429 Too Many Requests",
			"ERROR: exceeded retry limit, last status: 429",
		):
			stderr, prompt = self.realistic(line)
			self.assertKind(base.KIND_USAGE_LIMIT, stderr, prompt=prompt)

	def test_wordBoundaries(self):
		for text in (
			"ERROR: failed while designing in the editor",
			"ERROR: the dialog in front failed",
			"ERROR: signing failed",
			"ERROR: catalogin error",
		):
			self.assertKind(base.KIND_OTHER, text)

	def test_numbersNeedContext(self):
		self.assertKind(base.KIND_OTHER, "ERROR: stream ended\ntokens used\n28,401")
		self.assertKind(base.KIND_OTHER, "ERROR: item 429 failed to parse")
		self.assertKind(base.KIND_OTHER, "ERROR: request 401 of 500 failed")

	def test_notInstalled(self):
		self.assertKind(
			base.KIND_NOT_INSTALLED,
			"'node' is not recognized as an internal or external command,\noperable program or batch file.",
		)

	def test_otherDetailIsTechnicalAndMessageIsNot(self):
		stderr = "x" * 1000 + "\nERROR: model gpt-9 is not supported"
		error = self.assertKind(base.KIND_OTHER, stderr)
		self.assertEqual(error.message, "Codex could not answer (exit code 1).")
		self.assertTrue(error.detail.endswith("model gpt-9 is not supported"))
		self.assertLessEqual(len(error.detail), 300)

	def test_diagnosticLines(self):
		stderr, prompt = self.realistic("ERROR: boom\nwarning: trailing note")
		errorLines, tail = codex.diagnosticLines(stderr, prompt)
		self.assertEqual(errorLines, ["ERROR: boom"])
		self.assertEqual(tail, ["warning: trailing note"])


class TestLoginStatusParsing(unittest.TestCase):
	def test_parse(self):
		self.assertEqual(codex.parseLoginStatus(0, "Logged in using ChatGPT\n"), (True, "Logged in using ChatGPT"))
		loggedIn, text = codex.parseLoginStatus(
			1,
			"WARNING: proceeding, even though we could not create PATH aliases\nNot logged in\n",
		)
		self.assertFalse(loggedIn)
		self.assertEqual(text, "Not logged in")
		self.assertFalse(codex.parseLoginStatus(0, "Not logged in")[0])
		self.assertFalse(codex.parseLoginStatus(1, "Logged in using ChatGPT")[0])

	def test_apiKeyLogin(self):
		self.assertTrue(codex.isApiKeyLogin("Logged in using an API key - sk-proj-***"))
		self.assertFalse(codex.isApiKeyLogin("Logged in using ChatGPT"))


class TestBuildPrompt(unittest.TestCase):
	def test_withoutHistory(self):
		prompt = base.buildPrompt("  Why is the sky blue? ")
		self.assertTrue(prompt.startswith(base.DEFAULT_INSTRUCTIONS))
		self.assertNotIn("Earlier conversation:", prompt)
		self.assertTrue(prompt.rstrip().endswith("Question: Why is the sky blue?"))
		self.assertIn("Do not run commands", prompt)
		self.assertIn("The user is blind and uses a screen reader.", prompt)

	def test_blankInstructionsUseDefault(self):
		self.assertTrue(base.buildPrompt("q", instructions="  ").startswith(base.DEFAULT_INSTRUCTIONS))
		self.assertTrue(base.buildPrompt("q", instructions="Be brief.").startswith("Be brief."))

	def test_historyFormatAndOrder(self):
		history = [("user", "What is 2+2?"), ("assistant", "4."), ("user", "And times 3?"), ("assistant", "12.")]
		prompt = base.buildPrompt("And minus 1?", history, "Rules.")
		self.assertEqual(
			prompt,
			"Rules.\n\nEarlier conversation:\nYou: What is 2+2?\nAssistant: 4.\nYou: And times 3?\nAssistant: 12.\n\n"
			"Question: And minus 1?\n",
		)

	def test_turnLimit(self):
		history = []
		for i in range(15):
			history.append(("user", "question %d" % i))
			history.append(("assistant", "answer %d" % i))
		prompt = base.buildPrompt("next", history, maxTurns=10)
		self.assertNotIn("question 4\n", prompt)
		self.assertNotIn("answer 4\n", prompt)
		self.assertIn("You: question 5\n", prompt)
		self.assertIn("Assistant: answer 14\n", prompt)

	def test_characterLimitDropsOldestFirst(self):
		history = [("user", "old " + "a" * 5000), ("assistant", "mid " + "b" * 5000), ("user", "new " + "c" * 5000)]
		prompt = base.buildPrompt("q", history, maxChars=12000)
		self.assertNotIn("old ", prompt)
		self.assertIn("mid ", prompt)
		self.assertIn("new ", prompt)

	def test_singleHugeTurnIsClipped(self):
		history = [("assistant", "start " + "z" * 50000 + " end")]
		prompt = base.buildPrompt("q", history, maxChars=12000)
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
		answer = client.ask(base.buildPrompt("caf\u00e9 \"quotes\" & pipes |"))
		self.assertEqual(answer, "ECHO:Question: caf\u00e9 \"quotes\" & pipes |")
		self.assertEqual([n for n in os.listdir(self.workDir) if n.startswith("answer-")], [])

	def test_timeoutKillsWholeProcessGroup(self):
		# C10: the grandchild must die too, not just codex.
		client = CodexClient(executablePath=self.exe, workDir=self.workDir, isWindows=False)
		client.timeout = 1.5
		with self.assertRaises(CodexError) as cm:
			client.ask("SLEEP please")
		self.assertEqual(cm.exception.kind, base.KIND_TIMEOUT)
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
