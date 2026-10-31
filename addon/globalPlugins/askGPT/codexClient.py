# -*- coding: UTF-8 -*-
# Ask GPT NVDA add-on: runs the official OpenAI Codex CLI, signed in with a ChatGPT account.
# Pure standard library, no NVDA imports, so it can be unit tested on any platform.
# Messages in this module are English defaults for logs and tests;
# the NVDA user interface maps CodexError.kind to translated messages.

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

DEFAULT_TIMEOUT = 180
MIN_TIMEOUT = 30
MAX_TIMEOUT = 600
REASONING_EFFORTS = ("low", "medium", "high")
DEFAULT_REASONING_EFFORT = "low"
MAX_HISTORY_TURNS = 10
MAX_HISTORY_CHARS = 12000

DEFAULT_INSTRUCTIONS = (
	"You are a helpful assistant. Answer the question directly. "
	"Do not run commands, read or modify files, or write code unless the user asks for code. "
	"Plain text only, no Markdown tables, headings or emoji. Be concise unless asked for detail. "
	"The user is blind and uses a screen reader."
)

# Error kinds.
KIND_NOT_INSTALLED = "notInstalled"
KIND_NOT_LOGGED_IN = "notLoggedIn"
KIND_USAGE_LIMIT = "usageLimit"
KIND_TIMEOUT = "timeout"
KIND_CANCELLED = "cancelled"
KIND_CONFIG = "config"
KIND_OTHER = "other"

INSTALL_HELP = (
	"Install Node.js, then run npm install -g @openai/codex in a command prompt, "
	"or download codex from github.com/openai/codex/releases. "
	"Then restart NVDA, or set the Codex executable path in the Ask GPT settings."
)

# Model names go on the command line, so only allow a conservative character set.
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,99}$")
# npm cmd-shim line, e.g.  "%dp0%\node_modules\@openai\codex\bin\codex.js" %*
_SHIM_SCRIPT = re.compile(r'"%~?dp0%?\\([^"]+?\.(?:js|cjs|mjs))"', re.IGNORECASE)

_NOT_INSTALLED_PATTERNS = (
	"is not recognized as an internal or external command",
	"node: not found",
	"cannot find module",
	"the system cannot find the path specified",
)
_USAGE_LIMIT_PATTERNS = (
	re.compile(r"usage limit", re.IGNORECASE),
	re.compile(r"rate limit", re.IGNORECASE),
	re.compile(r"(?<![\d,.])429(?![\d,])"),
	re.compile(r"too many requests", re.IGNORECASE),
)
_NOT_LOGGED_IN_PATTERNS = (
	re.compile(r"not logged in", re.IGNORECASE),
	re.compile(r"(?<![\d,.])401(?![\d,])"),
	re.compile(r"unauthori[sz]ed", re.IGNORECASE),
	re.compile(r"sign[ -]?in", re.IGNORECASE),
	re.compile(r"log[ -]?in", re.IGNORECASE),
)


class CodexError(Exception):
	"""A failure running Codex.
	@ivar kind: one of the KIND_* constants.
	@ivar message: an English, human readable message.
	@ivar detail: extra diagnostic text (for example the end of stderr), may be empty.
	"""

	def __init__(self, kind, message, detail=""):
		super().__init__(message)
		self.kind = kind
		self.message = message
		self.detail = detail or ""

	def __str__(self):
		return self.message


def clampTimeout(value):
	try:
		value = int(value)
	except (TypeError, ValueError):
		return DEFAULT_TIMEOUT
	return max(MIN_TIMEOUT, min(MAX_TIMEOUT, value))


def isValidModel(model):
	return not model or bool(_SAFE_MODEL.match(model))


# Executable discovery


def candidatePaths(env=None, isWindows=IS_WINDOWS):
	"""Well known install locations, most likely first."""
	env = os.environ if env is None else env
	paths = []
	if isWindows:
		appData = env.get("APPDATA")
		localAppData = env.get("LOCALAPPDATA")
		userProfile = env.get("USERPROFILE")
		if appData:
			paths.append(os.path.join(appData, "npm", "codex.cmd"))
			paths.append(os.path.join(appData, "npm", "codex.exe"))
		if localAppData:
			paths.append(os.path.join(localAppData, "Programs", "codex", "codex.exe"))
			paths.append(os.path.join(localAppData, "Programs", "OpenAI", "Codex", "codex.exe"))
			paths.append(os.path.join(localAppData, "Microsoft", "WinGet", "Links", "codex.exe"))
		if userProfile:
			paths.append(os.path.join(userProfile, ".local", "bin", "codex.exe"))
			paths.append(os.path.join(userProfile, "scoop", "shims", "codex.exe"))
	else:
		home = env.get("HOME")
		if home:
			paths.append(os.path.join(home, ".local", "bin", "codex"))
			paths.append(os.path.join(home, ".npm-global", "bin", "codex"))
		paths.append("/usr/local/bin/codex")
	return paths


def findCodex(userPath="", env=None, which=shutil.which, isFile=os.path.isfile, isWindows=IS_WINDOWS):
	"""Return the path of the Codex executable, or None.
	A configured path wins; then PATH; then well known locations.
	"""
	userPath = (userPath or "").strip().strip('"')
	if userPath:
		return userPath if isFile(userPath) else None
	found = which("codex")
	if found:
		if isWindows and not os.path.splitext(found)[1]:
			# On Windows the extensionless npm file is a POSIX shell script; prefer the .cmd next to it.
			cmdPath = found + ".cmd"
			found = cmdPath if isFile(cmdPath) else None
		if found:
			return found
	for path in candidatePaths(env, isWindows):
		if isFile(path):
			return path
	return None


def _readText(path):
	with open(path, "r", encoding="utf-8", errors="replace") as f:
		return f.read()


def resolveCmdShim(cmdPath, readText=_readText, which=shutil.which, isFile=os.path.isfile):
	"""Turn an npm .cmd shim into [node, script] so no cmd.exe is needed. Returns None if unresolvable."""
	try:
		content = readText(cmdPath)
	except OSError:
		return None
	match = _SHIM_SCRIPT.search(content)
	if not match:
		return None
	baseDir = os.path.dirname(cmdPath)
	script = os.path.join(baseDir, *re.split(r"[\\/]", match.group(1)))
	if not isFile(script):
		return None
	node = os.path.join(baseDir, "node.exe")
	if not isFile(node):
		node = which("node")
	if not node:
		return None
	return [node, script]


# Fixed -c overrides such as approval_policy="never" may contain balanced quotes.
_SAFE_QUOTED_OVERRIDE = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]*="[A-Za-z0-9._-]*"$')


def _quoteForCmd(args):
	"""Quote for cmd.exe /s /c "...". Only fixed arguments (never user text) are passed here."""
	for arg in args:
		unsafeQuote = '"' in arg and not _SAFE_QUOTED_OVERRIDE.match(arg)
		if unsafeQuote or any(ch in arg for ch in "%\r\n"):
			raise CodexError(
				KIND_CONFIG,
				"A path contains characters that cannot be passed safely to cmd.exe: %s" % arg,
			)
	return subprocess.list2cmdline(args)


def buildCommand(exePath, args, isWindows=IS_WINDOWS, resolveShim=resolveCmdShim):
	"""Build the argv (or, for a cmd.exe wrapped shim on Windows, the command line string)."""
	args = list(args)
	if isWindows and exePath.lower().endswith((".cmd", ".bat")):
		resolved = resolveShim(exePath)
		if resolved:
			return resolved + args
		inner = _quoteForCmd([exePath] + args)
		# The outer quotes are stripped by /s, leaving the inner command line intact.
		return 'cmd.exe /d /s /c "%s"' % inner
	return [exePath] + args


def buildExecArgs(outputFile, model="", reasoningEffort=""):
	"""Fixed codex exec arguments. The prompt is never on the command line; "-" reads it from stdin."""
	args = [
		"exec",
		"--skip-git-repo-check",
		"--sandbox",
		"read-only",
		"--color",
		"never",
		"-c",
		'approval_policy="never"',
		"-o",
		outputFile,
	]
	model = (model or "").strip()
	if model:
		if not isValidModel(model):
			raise CodexError(KIND_CONFIG, "The model name contains characters that are not allowed: %s" % model)
		args += ["-m", model]
	reasoningEffort = (reasoningEffort or "").strip().lower()
	if reasoningEffort:
		if reasoningEffort not in REASONING_EFFORTS:
			raise CodexError(KIND_CONFIG, "Unknown reasoning effort: %s" % reasoningEffort)
		args += ["-c", 'model_reasoning_effort="%s"' % reasoningEffort]
	args.append("-")
	return args


# Prompt construction


def _clip(text, limit):
	text = text.strip()
	if len(text) <= limit:
		return text
	return "..." + text[-(limit - 3):]


def buildPrompt(
	question,
	history=(),
	instructions=DEFAULT_INSTRUCTIONS,
	maxTurns=MAX_HISTORY_TURNS,
	maxChars=MAX_HISTORY_CHARS,
):
	"""Build the full prompt text.
	@param history: earlier messages as (role, text) with role "user" or "assistant", oldest first.
	A turn is one question plus its answer; only the newest maxTurns turns (2 * maxTurns messages)
	and at most about maxChars characters of them are kept, dropping the oldest first.
	"""
	instructions = (instructions or "").strip() or DEFAULT_INSTRUCTIONS
	recent = list(history)[-2 * maxTurns:] if maxTurns > 0 else []
	lines = []
	for role, text in recent:
		text = (text or "").strip()
		if not text:
			continue
		speaker = "You" if role == "user" else "GPT"
		lines.append("%s: %s" % (speaker, text))
	kept = []
	total = 0
	for line in reversed(lines):
		if total + len(line) + 1 > maxChars:
			remaining = maxChars - total
			if not kept and remaining > 100:
				kept.append(_clip(line, remaining))
			break
		kept.append(line)
		total += len(line) + 1
	kept.reverse()
	parts = [instructions]
	if kept:
		parts.append("Earlier conversation:\n" + "\n".join(kept))
	parts.append("Question: %s" % (question or "").strip())
	return "\n\n".join(parts) + "\n"


# Error classification


def _removeEchoedPrompt(text, prompt):
	"""Codex echoes the prompt on stderr; drop those lines so user words cannot skew classification."""
	if not prompt:
		return text
	promptLines = {line.strip() for line in prompt.splitlines() if line.strip()}
	return "\n".join(line for line in text.splitlines() if line.strip() not in promptLines)


def _tail(text, limit=300):
	text = (text or "").strip()
	return text[-limit:] if len(text) > limit else text


def classifyFailure(returnCode, stdout, stderr, prompt=""):
	"""Map a failed Codex run to a CodexError. The matching is heuristic: Codex has no stable error codes."""
	stderr = _removeEchoedPrompt(stderr or "", prompt)
	stdout = _removeEchoedPrompt(stdout or "", prompt)
	combined = "%s\n%s" % (stderr, stdout)
	lower = combined.lower()
	detail = _tail(stderr) or _tail(stdout)
	if any(pattern in lower for pattern in _NOT_INSTALLED_PATTERNS):
		return CodexError(KIND_NOT_INSTALLED, "Codex could not be started. " + INSTALL_HELP, detail)
	if any(pattern.search(combined) for pattern in _USAGE_LIMIT_PATTERNS):
		return CodexError(
			KIND_USAGE_LIMIT,
			"You have hit the usage limit of your ChatGPT plan. Please try again later.",
			detail,
		)
	if any(pattern.search(combined) for pattern in _NOT_LOGGED_IN_PATTERNS):
		return CodexError(
			KIND_NOT_LOGGED_IN,
			"Codex is not signed in to ChatGPT. Use Sign in with ChatGPT in the Ask GPT settings.",
			detail,
		)
	return CodexError(
		KIND_OTHER,
		"Codex failed (exit code %s). %s" % (returnCode, detail),
		detail,
	)


def parseLoginStatus(returnCode, text):
	"""Return (loggedIn, cleanedText) from `codex login status` output."""
	lines = [
		line.strip()
		for line in (text or "").splitlines()
		if line.strip() and not line.strip().upper().startswith("WARNING")
	]
	cleaned = "\n".join(lines)
	lower = cleaned.lower()
	loggedIn = returnCode == 0 and "logged in" in lower and "not logged in" not in lower
	return loggedIn, cleaned


def isApiKeyLogin(statusText):
	"""True if Codex is signed in with an API key (billed per use) instead of a ChatGPT account."""
	return "api key" in (statusText or "").lower()


# Process handling


def _decode(data):
	if not data:
		return ""
	return data.decode("utf-8", errors="replace")


def killProcessTree(proc, isWindows=IS_WINDOWS, run=subprocess.run):
	"""Kill a process and its children. Never raises."""
	if proc is None:
		return
	try:
		if proc.poll() is not None:
			return
	except Exception:
		pass
	if isWindows:
		try:
			run(
				["taskkill", "/T", "/F", "/PID", str(proc.pid)],
				stdin=subprocess.DEVNULL,
				stdout=subprocess.DEVNULL,
				stderr=subprocess.DEVNULL,
				creationflags=CREATE_NO_WINDOW,
				timeout=10,
			)
		except Exception:
			pass
	try:
		proc.kill()
	except Exception:
		pass


class CodexClient:
	"""Runs Codex as a child process. All methods block; call them from a background thread."""

	def __init__(
		self,
		executablePath="",
		workDir=None,
		model="",
		reasoningEffort=DEFAULT_REASONING_EFFORT,
		timeout=DEFAULT_TIMEOUT,
		popen=subprocess.Popen,
		isWindows=IS_WINDOWS,
		finder=findCodex,
	):
		self.executablePath = (executablePath or "").strip()
		self.workDir = workDir or os.path.join(tempfile.gettempdir(), "askGPT-workdir")
		self.model = (model or "").strip()
		self.reasoningEffort = (reasoningEffort or "").strip().lower()
		self.timeout = clampTimeout(timeout)
		self._popen = popen
		self._isWindows = isWindows
		self._finder = finder
		self._loginProcess = None

	# Helpers

	def locate(self):
		"""Return the Codex executable path or raise CodexError(notInstalled)."""
		path = self._finder(self.executablePath)
		if not path:
			if self.executablePath:
				message = "The Codex executable path in the settings does not exist: %s" % self.executablePath
			else:
				message = "The OpenAI Codex CLI was not found. " + INSTALL_HELP
			raise CodexError(KIND_NOT_INSTALLED, message)
		return path

	def _command(self, args):
		return buildCommand(self.locate(), args, isWindows=self._isWindows)

	def _popenKwargs(self, cwd):
		kwargs = {
			"stdin": subprocess.PIPE,
			"stdout": subprocess.PIPE,
			"stderr": subprocess.PIPE,
			"cwd": cwd,
		}
		if self._isWindows:
			kwargs["creationflags"] = CREATE_NO_WINDOW
		env = dict(os.environ)
		env["NO_COLOR"] = "1"
		kwargs["env"] = env
		return kwargs

	def _ensureWorkDir(self):
		try:
			os.makedirs(self.workDir, exist_ok=True)
		except OSError as e:
			raise CodexError(KIND_OTHER, "Could not create the working directory %s: %s" % (self.workDir, e))
		return self.workDir

	def _run(self, args, inputText=None, timeout=None, cancelEvent=None):
		"""Run codex with args. Returns (returnCode, stdoutText, stderrText)."""
		command = self._command(args)
		timeout = self.timeout if timeout is None else timeout
		cwd = self._ensureWorkDir()
		try:
			proc = self._popen(command, **self._popenKwargs(cwd))
		except FileNotFoundError:
			raise CodexError(KIND_NOT_INSTALLED, "The OpenAI Codex CLI could not be started. " + INSTALL_HELP)
		except OSError as e:
			raise CodexError(KIND_OTHER, "Codex could not be started: %s" % e)
		output = {"stdout": b"", "stderr": b""}

		def reader(stream, key):
			try:
				output[key] = stream.read()
			except Exception:
				pass

		def writer():
			try:
				if inputText is not None:
					proc.stdin.write(inputText.encode("utf-8"))
			except Exception:
				pass  # The process may exit early; its exit status reports the problem.
			finally:
				try:
					proc.stdin.close()
				except Exception:
					pass

		readers = [
			threading.Thread(target=reader, args=(proc.stdout, "stdout"), daemon=True),
			threading.Thread(target=reader, args=(proc.stderr, "stderr"), daemon=True),
		]
		threads = readers + [threading.Thread(target=writer, daemon=True)]
		for thread in threads:
			thread.start()
		deadline = time.monotonic() + timeout
		failure = None
		try:
			while True:
				try:
					proc.wait(timeout=0.2)
					break
				except subprocess.TimeoutExpired:
					pass
				if cancelEvent is not None and cancelEvent.is_set():
					failure = CodexError(KIND_CANCELLED, "The request was cancelled.")
				elif time.monotonic() >= deadline:
					failure = CodexError(
						KIND_TIMEOUT,
						"Codex did not answer within %d seconds. "
						"Try again, or increase the timeout in the settings." % timeout,
					)
				if failure is not None:
					killProcessTree(proc, self._isWindows)
					break
		finally:
			self._cleanUp(proc, threads, readers)
		if failure is not None:
			raise failure
		return proc.returncode, _decode(output["stdout"]), _decode(output["stderr"])

	@staticmethod
	def _cleanUp(proc, threads, readers):
		"""Wait for the I/O threads, close finished pipes and reap the process."""
		for thread in threads:
			thread.join(5)
		for thread, stream in zip(readers, (proc.stdout, proc.stderr)):
			# Closing a pipe that another thread is still reading from could block, so skip those.
			if not thread.is_alive():
				try:
					stream.close()
				except Exception:
					pass
		try:
			proc.wait(timeout=5)
		except Exception:
			pass

	# Public API

	def ask(self, prompt, cancelEvent=None):
		"""Send prompt (already built with buildPrompt) and return the answer text."""
		self._ensureWorkDir()
		fd, outputFile = tempfile.mkstemp(prefix="answer-", suffix=".txt", dir=self.workDir)
		os.close(fd)
		try:
			args = buildExecArgs(outputFile, self.model, self.reasoningEffort)
			returnCode, stdout, stderr = self._run(args, inputText=prompt, cancelEvent=cancelEvent)
			if returnCode != 0:
				raise classifyFailure(returnCode, stdout, stderr, prompt)
			try:
				answer = _readText(outputFile).strip()
			except OSError:
				answer = ""
			if not answer:
				answer = stdout.strip()
			if not answer:
				raise CodexError(KIND_OTHER, "Codex returned an empty answer.", _tail(stderr))
			return answer
		finally:
			try:
				os.remove(outputFile)
			except OSError:
				pass

	def loginStatus(self):
		"""Return (loggedIn, text) from `codex login status`."""
		returnCode, stdout, stderr = self._run(["login", "status"], timeout=30)
		return parseLoginStatus(returnCode, "%s\n%s" % (stdout, stderr))

	def login(self):
		"""Start `codex login` without waiting. It opens the default browser for ChatGPT sign-in.
		Returns the process so the caller can wait for it in a background thread.
		"""
		self.cancelLogin()
		command = self._command(["login"])
		kwargs = {
			"stdin": subprocess.DEVNULL,
			"stdout": subprocess.DEVNULL,
			"stderr": subprocess.DEVNULL,
			"cwd": self._ensureWorkDir(),
		}
		if self._isWindows:
			kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
		try:
			self._loginProcess = self._popen(command, **kwargs)
		except FileNotFoundError:
			raise CodexError(KIND_NOT_INSTALLED, "The OpenAI Codex CLI could not be started. " + INSTALL_HELP)
		except OSError as e:
			raise CodexError(KIND_OTHER, "Codex could not be started: %s" % e)
		return self._loginProcess

	def cancelLogin(self):
		if self._loginProcess is not None:
			killProcessTree(self._loginProcess, self._isWindows)
			self._loginProcess = None

	def logout(self):
		returnCode, stdout, stderr = self._run(["logout"], timeout=30)
		text = "\n".join(
			line.strip()
			for line in ("%s\n%s" % (stdout, stderr)).splitlines()
			if line.strip() and not line.strip().upper().startswith("WARNING")
		)
		return returnCode == 0, text

	def version(self):
		returnCode, stdout, stderr = self._run(["--version"], timeout=30)
		if returnCode != 0:
			raise classifyFailure(returnCode, stdout, stderr)
		return stdout.strip() or stderr.strip()
