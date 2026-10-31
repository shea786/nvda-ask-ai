"""Tests for the Claude Code provider and the provider registry (no NVDA, never runs the real Claude Code)."""

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import helpers
from helpers import FakeJob, FakePopen

import providers
from providers import base, claude, codex
from providers.claude import ClaudeProvider

NASTY_QUESTION = 'What is "2 & 3" | echo %PATH% ^ && del C:\\ ; $(rm -rf /) ?'
FAKE_EXE = os.path.abspath("fake-claude")
WIN_ENV = {"SystemRoot": "C:\\Windows", "ProgramFiles": "C:\\Program Files"}

# Shapes printed by Claude Code 2.1.314 with --output-format json. NOT_LOGGED_IN was captured from a real
# run without credentials (CLAUDE_CONFIG_DIR pointing at an empty folder); the others follow its shape.
NOT_LOGGED_IN = {
	"duration_api_ms": 0,
	"stop_reason": "stop_sequence",
	"session_id": "fcab1237-01f3-41a5-b671-cd85973ea5a9",
	"total_cost_usd": 0,
	"usage": {"input_tokens": 0, "output_tokens": 0, "service_tier": "standard"},
	"modelUsage": {},
	"permission_denials": [],
	"terminal_reason": "api_error",
	"is_error": True,
	"num_turns": 1,
	"subtype": "success",
	"api_error_status": None,
	"result": "Not logged in \u00b7 Please run /login",
	"type": "result",
	"duration_ms": 171,
	"uuid": "b1ef3ee5-dac9-44db-8d87-6d382b71e254",
}
SUCCESS = dict(
	NOT_LOGGED_IN,
	is_error=False,
	terminal_reason="completed",
	result="**Paris** is the capital of France.",
	total_cost_usd=0.0123,
	usage={"input_tokens": 1520, "output_tokens": 12, "service_tier": "standard"},
)


def result(**changes):
	data = dict(NOT_LOGGED_IN)
	data.update(changes)
	return (json.dumps(data) + "\n").encode("utf-8")


def makeClient(popen, workDir, **kwargs):
	kwargs.setdefault("isWindows", False)
	kwargs.setdefault("killpg", mock.Mock())
	kwargs.setdefault("env", {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-ant-secret", "ANTHROPIC_AUTH_TOKEN": "tok"})
	if kwargs["isWindows"]:
		kwargs.setdefault("jobFactory", FakeJob)
	return ClaudeProvider(
		executablePath=FAKE_EXE,
		workDir=workDir,
		popen=popen,
		finder=lambda userPath: FAKE_EXE,
		**kwargs,
	)


class TempDirTestCase(unittest.TestCase):
	def setUp(self):
		self.workDir = tempfile.mkdtemp(prefix="askai-claude-test-")
		self.addCleanup(shutil.rmtree, self.workDir, True)


class TestRegistry(unittest.TestCase):
	def test_providers(self):
		self.assertEqual(providers.ids(), ["codex", "claude"])
		self.assertEqual(providers.DEFAULT_PROVIDER, "codex")
		self.assertEqual(providers.displayName("codex"), "ChatGPT (Codex)")
		self.assertEqual(providers.displayName("claude"), "Claude (Claude Code)")
		self.assertEqual(providers.shortName("claude"), "Claude")
		self.assertIs(providers.get("claude"), ClaudeProvider)
		self.assertIs(providers.get("someday"), codex.CodexProvider, "unknown ids fall back to the default")
		self.assertFalse(providers.isKnown("someday"))
		self.assertIsInstance(providers.create("claude", workDir="/tmp/x"), ClaudeProvider)

	def test_interface(self):
		for cls in providers.PROVIDER_CLASSES:
			for name in ("locate", "ask", "loginStatus", "login", "cancelLogin", "logout", "version", "models", "terminate"):
				self.assertTrue(callable(getattr(cls, name)), (cls, name))
			self.assertTrue(cls.displayName and cls.shortName and cls.programName and cls.installHelp)


class TestAskArguments(unittest.TestCase):
	def test_noProjectFolderMeansNoTools(self):
		args = claude.buildAskArgs()
		self.assertEqual(args[:2], ["-p", "--output-format"])
		self.assertEqual(args[args.index("--output-format") + 1], "json")
		self.assertEqual(args[args.index("--tools") + 1], "")
		self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
		for flag in ("--no-session-persistence", "--strict-mcp-config", "--restricted"):
			self.assertIn(flag, args)
		self.assertEqual(args[args.index("--permission-prompts") + 1], "none")
		self.assertEqual(args[args.index("--disallowedTools") + 1], "mcp__*")

	def test_readOnlyProjectFolder(self):
		args = claude.buildAskArgs(projectFolder="C:\\Work", allowWrites=False)
		self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep")
		self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")

	def test_writableProjectFolder(self):
		args = claude.buildAskArgs(projectFolder="C:\\Work", allowWrites=True)
		self.assertEqual(args[args.index("--tools") + 1], "Read,Write,Edit,Glob,Grep")
		self.assertEqual(args[args.index("--permission-mode") + 1], "acceptEdits")

	def test_neverDangerousToolsOrBypass(self):
		for folder, writes in (("", False), ("", True), ("C:\\W", False), ("C:\\W", True)):
			args = claude.buildAskArgs("opus", "high", folder, writes)
			joined = " ".join(args)
			for forbidden in ("Bash", "PowerShell", "WebFetch", "WebSearch", "Task", "bypassPermissions",
					"dangerously", "--allowedTools", "--append-system-prompt", "--system-prompt", "--add-dir"):
				self.assertNotIn(forbidden, joined, (folder, writes))
			self.assertNotIn("C:\\W", joined, "the folder is the working directory, not an argument")

	def test_listOptionsAreFollowedByOptions(self):
		# --tools and --disallowedTools take several values; each must be followed by another option so it
		# can never swallow a following argument (there is no positional prompt at all).
		args = claude.buildAskArgs("sonnet", "low", "C:\\W", True)
		for flag in ("--tools", "--disallowedTools"):
			following = args[args.index(flag) + 2]
			self.assertTrue(following.startswith("--"), (flag, following))
		self.assertTrue(all(arg.startswith("-") for arg in args[::2][:1]))

	def test_modelAndEffort(self):
		args = claude.buildAskArgs("opus[1m]", "xhigh")
		self.assertEqual(args[args.index("--model") + 1], "opus[1m]")
		self.assertEqual(args[args.index("--effort") + 1], "xhigh")
		self.assertNotIn("--model", claude.buildAskArgs())
		self.assertNotIn("--effort", claude.buildAskArgs())
		with self.assertRaises(base.ProviderError) as cm:
			claude.buildAskArgs("opus; rm -rf /")
		self.assertEqual((cm.exception.kind, cm.exception.provider), (base.KIND_CONFIG, "claude"))
		with self.assertRaises(base.ProviderError):
			claude.buildAskArgs(effort="max power")

	def test_effortsAndModels(self):
		self.assertEqual(claude.REASONING_EFFORTS, ("low", "medium", "high", "xhigh", "max"))
		self.assertEqual([m.slug for m in claude.presetModels()], ["sonnet", "opus", "haiku", "fable"])
		self.assertTrue(all(m.efforts == claude.REASONING_EFFORTS for m in claude.presetModels()))


class TestAsk(TempDirTestCase):
	def test_promptGoesToStdinAndNeverToArgv(self):
		popen = FakePopen(stdout=(json.dumps(SUCCESS) + "\n").encode("utf-8"))
		client = makeClient(popen, self.workDir)
		prompt = base.buildPrompt(NASTY_QUESTION)
		self.assertEqual(client.ask(prompt), "**Paris** is the capital of France.")
		argv = popen.lastArgv
		self.assertEqual(argv[0], FAKE_EXE)
		joined = " ".join(argv)
		for fragment in ("2 & 3", "PATH", "Question", "helpful assistant"):
			self.assertNotIn(fragment, joined)
		self.assertEqual(popen.lastProcess.stdin.captured.decode("utf-8"), prompt)
		self.assertEqual(client.lastMeta["sessionId"], SUCCESS["session_id"])
		self.assertEqual(client.lastMeta["usage"]["output_tokens"], 12)
		self.assertEqual(client.lastMeta["totalCostUsd"], 0.0123)

	def test_environmentNeverBillsTheApi(self):
		popen = FakePopen(stdout=(json.dumps(SUCCESS)).encode("utf-8"))
		makeClient(popen, self.workDir).ask("hi")
		env = popen.calls[-1][1]["env"]
		self.assertNotIn("ANTHROPIC_API_KEY", env)
		self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
		self.assertEqual(env["PATH"], "/usr/bin")
		self.assertEqual(env["NO_COLOR"], "1")

	def test_windowsEnvironmentStrippingIsCaseInsensitive(self):
		env = base.childEnvironment(
			{"Anthropic_Api_Key": "x", "anthropic_auth_token": "y", "Path": "C:\\"},
			claude.STRIPPED_ENVIRONMENT,
			isWindows=True,
		)
		self.assertEqual(env, {"Path": "C:\\", "NO_COLOR": "1"})

	def test_workingDirectory(self):
		popen = FakePopen(stdout=json.dumps(SUCCESS).encode("utf-8"))
		makeClient(popen, self.workDir).ask("hi")
		self.assertEqual(popen.calls[-1][1]["cwd"], self.workDir)
		project = tempfile.mkdtemp(prefix="askai-project-")
		self.addCleanup(shutil.rmtree, project, True)
		makeClient(popen, self.workDir, projectFolder=project, allowProjectWrites=True).ask("hi")
		self.assertEqual(popen.calls[-1][1]["cwd"], project)
		self.assertEqual(popen.lastArgv[popen.lastArgv.index("--tools") + 1], "Read,Write,Edit,Glob,Grep")
		# ask() arguments override the constructor's.
		makeClient(popen, self.workDir).ask("hi", projectFolder=project, allowWrites=False, model="haiku", effort="max")
		argv = popen.lastArgv
		self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep")
		self.assertEqual((argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]), ("haiku", "max"))

	def test_missingProjectFolder(self):
		popen = FakePopen(stdout=json.dumps(SUCCESS).encode("utf-8"))
		with self.assertRaises(base.ProviderError) as cm:
			makeClient(popen, self.workDir, projectFolder=os.path.join(self.workDir, "gone")).ask("hi")
		self.assertEqual(cm.exception.reason, base.REASON_PROJECT_MISSING)
		self.assertEqual(popen.calls, [])

	def test_notLoggedIn(self):
		popen = FakePopen(returncode=1, stdout=result())
		with self.assertRaises(base.ProviderError) as cm:
			makeClient(popen, self.workDir).ask("hi")
		self.assertEqual((cm.exception.kind, cm.exception.provider), (base.KIND_NOT_LOGGED_IN, "claude"))
		self.assertIn("Not logged in", cm.exception.detail)

	def test_emptyAnswer(self):
		popen = FakePopen(stdout=json.dumps(dict(SUCCESS, result="  ")).encode("utf-8"))
		with self.assertRaises(base.ProviderError) as cm:
			makeClient(popen, self.workDir).ask("hi")
		self.assertEqual(cm.exception.reason, base.REASON_EMPTY_ANSWER)

	def test_noJsonIsClassifiedFromStderr(self):
		popen = FakePopen(returncode=1, stdout=b"", stderr=b"error: unknown option '--restricted'\n")
		with self.assertRaises(base.ProviderError) as cm:
			makeClient(popen, self.workDir).ask("hi")
		self.assertEqual((cm.exception.kind, cm.exception.reason), (base.KIND_CONFIG, base.REASON_OUTDATED))

	def test_cancelAndTimeoutKillTheProcess(self):
		import threading

		popen = FakePopen(hang=True)
		cancel = threading.Event()
		cancel.set()
		with self.assertRaises(base.ProviderError) as cm:
			makeClient(popen, self.workDir).ask("hi", cancelEvent=cancel)
		self.assertEqual(cm.exception.kind, base.KIND_CANCELLED)
		self.assertTrue(popen.lastProcess.killed)


class TestParseAndClassify(unittest.TestCase):
	def classify(self, text="", stderr="", status=None, returnCode=1):
		stdout = result(result=text, api_error_status=status) if text or status else b""
		return claude.classifyFailure(returnCode, stdout.decode("utf-8"), stderr)

	def test_parseResult(self):
		parsed = claude.parseResult(json.dumps(SUCCESS))
		self.assertFalse(parsed.failed)
		self.assertEqual(parsed.text, SUCCESS["result"])
		# Warning lines before the JSON are tolerated.
		parsed = claude.parseResult("Warning: something\n" + json.dumps(NOT_LOGGED_IN) + "\n")
		self.assertTrue(parsed.failed)
		self.assertIsNone(claude.parseResult(""))
		self.assertIsNone(claude.parseResult("not json"))
		self.assertTrue(claude.parseResult(json.dumps(dict(SUCCESS, subtype="error_max_turns", is_error=False))).failed)

	def test_notLoggedIn(self):
		for text in (
			"Not logged in \u00b7 Please run /login",
			"Invalid API key \u00b7 Please run /login",
			"OAuth token revoked \u00b7 Please run /login",
			"Login expired \u00b7 Please run /login",
		):
			self.assertEqual(self.classify(text).kind, base.KIND_NOT_LOGGED_IN, text)
		self.assertEqual(self.classify("API Error", status=401).kind, base.KIND_NOT_LOGGED_IN)

	def test_usageLimitWithResetText(self):
		error = self.classify("You've hit your session limit \u00b7 resets 3pm (Europe/London)")
		self.assertEqual(error.kind, base.KIND_USAGE_LIMIT)
		self.assertEqual(error.resetText, "3pm (Europe/London)")
		self.assertIsNone(error.resetEpoch)
		error = self.classify("You've hit your weekly limit \u00b7 resets Nov 3, 9am (America/New_York)")
		self.assertEqual(error.resetText, "Nov 3, 9am (America/New_York)")

	def test_usageLimitWithEpoch(self):
		error = self.classify("Claude AI usage limit reached|1793451600")
		self.assertEqual(error.kind, base.KIND_USAGE_LIMIT)
		self.assertEqual(error.resetEpoch, 1793451600.0)
		self.assertNotIn("|", error.message)
		self.assertEqual(self.classify("Claude AI usage limit reached|1793451600000").resetEpoch, 1793451600.0)
		moment = time.mktime((2026, 10, 31, 15, 0, 0, 0, 0, -1))
		self.assertEqual(claude.formatResetTime(moment, now=moment - 3600), "15:00")
		self.assertEqual(claude.formatResetTime(moment, now=moment - 3 * 86400), "2026-10-31 15:00")

	def test_busyIsNotTheUsersLimit(self):
		error = self.classify("Server is temporarily limiting requests (not your usage limit)")
		self.assertEqual((error.kind, error.reason), (base.KIND_OTHER, base.REASON_BUSY))
		self.assertEqual(self.classify("API Error: Overloaded", status=529).reason, base.REASON_BUSY)
		self.assertEqual(self.classify("API Error", status=429).kind, base.KIND_USAGE_LIMIT)

	def test_network(self):
		for text in ("API Error: Connection error.", "Request timed out.", "getaddrinfo ENOTFOUND api.anthropic.com"):
			self.assertEqual(self.classify(text).kind, base.KIND_NETWORK, text)

	def test_notInstalledAndOutdated(self):
		error = claude.classifyFailure(1, "", "'claude' is not recognized as an internal or external command")
		self.assertEqual(error.kind, base.KIND_NOT_INSTALLED)
		error = claude.classifyFailure(1, "", "error: unknown option '--permission-prompts'")
		self.assertEqual(error.reason, base.REASON_OUTDATED)

	def test_questionTextIsNeverClassified(self):
		# A successful run's answer, and the user's question, are never inspected.
		prompt = "Question: why do I keep getting 'Not logged in' and usage limit errors?"
		error = claude.classifyFailure(2, "", "something odd\n" + prompt, prompt=prompt)
		self.assertEqual(error.kind, base.KIND_OTHER)
		self.assertEqual(error.message, "Claude Code could not answer (exit code 2).")


class TestLoginStatus(TempDirTestCase):
	# Shapes printed by `claude auth status` in Claude Code 2.1.314 (personal details removed).
	SUBSCRIPTION = {
		"loggedIn": True,
		"authMethod": "claude.ai",
		"apiProvider": "firstParty",
		"analyticsDisabled": False,
		"email": "someone@example.com",
		"orgId": "00000000-0000-0000-0000-000000000000",
		"orgName": "Someone",
		"subscriptionType": "max",
	}
	SIGNED_OUT = {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty", "analyticsDisabled": False}

	def test_states(self):
		state, text = claude.loginState(0, json.dumps(self.SUBSCRIPTION))
		self.assertEqual((state, text), (base.STATE_SUBSCRIPTION, "Claude Max subscription"))
		self.assertNotIn("someone@example.com", text)
		self.assertEqual(claude.loginState(1, json.dumps(self.SIGNED_OUT))[0], base.STATE_SIGNED_OUT)
		token = dict(self.SIGNED_OUT, loggedIn=True, authMethod="oauth_token")
		self.assertEqual(claude.loginState(0, json.dumps(token))[0], base.STATE_SUBSCRIPTION)
		for method in ("api_key", "api_key_helper", "third_party"):
			data = dict(self.SIGNED_OUT, loggedIn=True, authMethod=method)
			self.assertEqual(claude.loginState(0, json.dumps(data))[0], base.STATE_API_KEY, method)
		# --text output and older versions.
		self.assertEqual(claude.loginState(1, "Not logged in. Run claude auth login to authenticate.")[0], base.STATE_SIGNED_OUT)
		self.assertEqual(claude.loginState(0, "Login method: Claude Max account\nOrganization: X")[0], base.STATE_SUBSCRIPTION)
		self.assertEqual(claude.loginState(0, "API key: ANTHROPIC_API_KEY")[0], base.STATE_API_KEY)

	def test_commands(self):
		popen = FakePopen(returncode=0, stdout=json.dumps(self.SUBSCRIPTION).encode("utf-8"))
		client = makeClient(popen, self.workDir)
		self.assertEqual(client.loginStatus()[0], base.STATE_SUBSCRIPTION)
		self.assertEqual(popen.lastArgv, [FAKE_EXE, "auth", "status", "--json"])
		self.assertNotIn("ANTHROPIC_API_KEY", popen.calls[-1][1]["env"])
		popen = FakePopen(returncode=0, stdout=b"Successfully logged out from your Anthropic account.\n")
		client = makeClient(popen, self.workDir)
		self.assertEqual(client.logout(), (True, "Successfully logged out from your Anthropic account."))
		self.assertEqual(popen.lastArgv, [FAKE_EXE, "auth", "logout"])
		popen = FakePopen(returncode=0, stdout=b"2.1.314 (Claude Code)\n")
		self.assertEqual(makeClient(popen, self.workDir).version(), "2.1.314 (Claude Code)")

	def test_loginCapturesLinkAndAcceptsACode(self):
		# The exact output of `claude auth login` 2.1.314 with stdout piped.
		output = (
			"Opening browser to sign in\u2026\n"
			"If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&client_id=x"
			"&response_type=code&state=abc\n"
			"Paste code here if prompted > "
		).encode("utf-8")
		popen = FakePopen(stdout=output, hang=True)
		client = makeClient(popen, self.workDir)
		urls = []
		proc = client.login(onUrl=urls.append)
		self.assertEqual(popen.lastArgv, [FAKE_EXE, "auth", "login", "--claudeai"])
		options = popen.calls[-1][1]
		self.assertEqual(options["stdin"], -1, "stdin is a pipe so a sign-in code can be typed in")
		for _ in range(100):
			if urls:
				break
			time.sleep(0.01)
		self.assertEqual(urls, ["https://claude.com/cai/oauth/authorize?code=true&client_id=x&response_type=code&state=abc"])
		self.assertEqual(client.loginUrl, urls[0])
		self.assertTrue(client.submitLoginCode("  theCode#theState \n"))
		self.assertEqual(proc.stdin.getvalue(), b"theCode#theState\n")
		self.assertFalse(client.submitLoginCode("two words"))
		self.assertFalse(client.submitLoginCode(""))
		client.cancelLogin()
		self.assertTrue(proc.killed)
		self.assertFalse(client.submitLoginCode("late#code"), "no sign-in running")

	def test_codexLoginHasNoCodeInput(self):
		popen = FakePopen(stdout=b"", hang=True)
		client = codex.CodexProvider(executablePath="x", workDir=self.workDir, popen=popen, isWindows=False,
			finder=lambda p: "codex", killpg=mock.Mock())
		client.login()
		self.assertEqual(popen.calls[-1][1]["stdin"], -3)  # subprocess.DEVNULL
		self.assertFalse(client.submitLoginCode("a#b"))
		client.cancelLogin()

	def test_extractUrlIgnoresTerminalEscapes(self):
		text = "visit: \x1b]8;;https://claude.com/x?a=1\x07https://claude.com/x?a=1\x1b]8;;\x07\n"
		self.assertEqual(base.extractUrl(text), "https://claude.com/x?a=1")


class TestLocate(unittest.TestCase):
	def test_windowsCandidates(self):
		env = {"USERPROFILE": "C:\\Users\\Jo", "APPDATA": "C:\\Users\\Jo\\AppData\\Roaming", "LOCALAPPDATA": "C:\\Users\\Jo\\AppData\\Local"}
		self.assertEqual(
			claude.candidatePaths(env, isWindows=True),
			[
				"C:\\Users\\Jo\\.local\\bin\\claude.exe",
				"C:\\Users\\Jo\\AppData\\Roaming\\npm\\claude.cmd",
				"C:\\Users\\Jo\\AppData\\Local\\Microsoft\\WinGet\\Links\\claude.exe",
			],
		)
		found = claude.findClaude(env=env, which=lambda n: None, isFile=lambda p: p.endswith("claude.cmd"), isWindows=True)
		self.assertEqual(found, "C:\\Users\\Jo\\AppData\\Roaming\\npm\\claude.cmd")
		self.assertEqual(claude.findClaude('"D:\\c\\claude.exe"', which=lambda n: "x", isFile=lambda p: True), "D:\\c\\claude.exe")
		self.assertEqual(claude.findClaude(which=lambda n: "C:\\bin\\claude.exe", isFile=lambda p: True, isWindows=True), "C:\\bin\\claude.exe")

	def test_npmShimForANativeProgram(self):
		# npm's cmd-shim for @anthropic-ai/claude-code, whose bin is bin/claude.exe (no Node.js at run time).
		shim = (
			"@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\nSETLOCAL\r\nCALL :find_dp0\r\n"
			'"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"   %*\r\n'
		)
		cmdPath = "C:\\Users\\Jo\\AppData\\Roaming\\npm\\claude.cmd"
		exe = "C:\\Users\\Jo\\AppData\\Roaming\\npm\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"
		resolved = base.resolveCmdShim(cmdPath, readText=lambda p: shim, which=lambda n: None, isFile=lambda p: p == exe, env=WIN_ENV)
		self.assertEqual(resolved, [exe])
		with self.assertRaises(base.ProviderError) as cm:
			base.resolveCmdShim(
				cmdPath,
				readText=lambda p: shim,
				which=lambda n: None,
				isFile=lambda p: False,
				env=WIN_ENV,
				programName="Claude Code",
				npmPackage="@anthropic-ai/claude-code",
				provider="claude",
			)
		self.assertEqual((cm.exception.reason, cm.exception.provider), (base.REASON_INCOMPLETE_INSTALL, "claude"))
		self.assertIn("@anthropic-ai/claude-code", cm.exception.message)

	def test_missingProgram(self):
		client = ClaudeProvider(workDir="/tmp", isWindows=False, finder=lambda p: None)
		with self.assertRaises(base.ProviderError) as cm:
			client.locate()
		self.assertEqual((cm.exception.kind, cm.exception.provider), (base.KIND_NOT_INSTALLED, "claude"))
		self.assertIn("irm https://claude.ai/install.ps1 | iex", cm.exception.message)
		client = ClaudeProvider(executablePath="D:\\nope\\claude.exe", workDir="/tmp", isWindows=False, finder=lambda p: None)
		with self.assertRaises(base.ProviderError) as cm:
			client.locate()
		self.assertEqual(cm.exception.reason, base.REASON_CUSTOM_PATH)

	def test_windowsCommandThroughCmdFallbackKeepsEmptyToolsArgument(self):
		command = base.buildCommand(
			"C:\\fake\\claude.cmd",
			claude.buildAskArgs(),
			isWindows=True,
			resolveShim=lambda path: None,
			env=WIN_ENV,
		)
		self.assertIn('"--tools" "" "--disallowedTools" "mcp__*"', command)


class TestCodexEnvironment(unittest.TestCase):
	def test_codexNeverUsesAnApiKeyFromTheEnvironment(self):
		workDir = tempfile.mkdtemp(prefix="askai-codex-env-")
		self.addCleanup(shutil.rmtree, workDir, True)
		popen = FakePopen(outputText="ok")
		client = codex.CodexProvider(
			executablePath="x",
			workDir=workDir,
			popen=popen,
			isWindows=False,
			finder=lambda p: "codex",
			killpg=mock.Mock(),
			env={"PATH": "/bin", "OPENAI_API_KEY": "sk-a", "CODEX_API_KEY": "sk-b", "HOME": "/home/x"},
		)
		client.ask("hi")
		env = popen.calls[-1][1]["env"]
		self.assertNotIn("OPENAI_API_KEY", env)
		self.assertNotIn("CODEX_API_KEY", env)
		self.assertEqual((env["PATH"], env["HOME"]), ("/bin", "/home/x"))

	def test_codexLoginState(self):
		self.assertEqual(codex.loginState(0, "Logged in using ChatGPT")[0], base.STATE_SUBSCRIPTION)
		self.assertEqual(codex.loginState(0, "Logged in using an API key - sk-proj-***")[0], base.STATE_API_KEY)
		self.assertEqual(codex.loginState(0, "Not logged in")[0], base.STATE_SIGNED_OUT)

	def test_projectInstructionsDifferPerService(self):
		self.assertIn("Word, PDF", codex.CodexProvider.projectInstructions(True))
		self.assertIn("cannot run commands", ClaudeProvider.projectInstructions(True))
		self.assertIn("Save answer as", ClaudeProvider.projectInstructions(True))
		self.assertIn("cannot create or change files", ClaudeProvider.projectInstructions(False))


if __name__ == "__main__":
	unittest.main()
