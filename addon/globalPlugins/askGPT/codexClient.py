# -*- coding: UTF-8 -*-
# Ask GPT NVDA add-on: runs the official OpenAI Codex CLI, signed in with a ChatGPT account.
# Pure standard library, no NVDA imports, so it can be unit tested on any platform.
# Messages in this module are English defaults for logs and tests;
# the NVDA user interface maps CodexError.kind / reason to translated messages.

import ctypes
import glob
import ntpath
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
SIGKILL = getattr(signal, "SIGKILL", 9)

DEFAULT_TIMEOUT = 180
MIN_TIMEOUT = 30
MAX_TIMEOUT = 600
REASONING_EFFORTS = ("low", "medium", "high")
DEFAULT_REASONING_EFFORT = "low"
MAX_HISTORY_TURNS = 10
MAX_HISTORY_CHARS = 12000
ANSWER_FILE_PREFIX = "answer-"
ANSWER_FILE_SUFFIX = ".txt"

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
KIND_NETWORK = "network"
KIND_TIMEOUT = "timeout"
KIND_CANCELLED = "cancelled"
KIND_CONFIG = "config"
KIND_OTHER = "other"

# Finer reasons within a kind, so the UI can give specific advice.
REASON_CUSTOM_PATH = "customPathMissing"
REASON_NODE_MISSING = "nodeMissing"
REASON_INCOMPLETE_INSTALL = "incompleteInstall"
REASON_EMPTY_ANSWER = "emptyAnswer"

INSTALL_HELP = (
	"Install Node.js, then run npm install -g @openai/codex in a command prompt, "
	"or download codex from github.com/openai/codex/releases. "
	"Then restart NVDA, or set the Codex executable path in the Ask GPT settings."
)

# Model names go on the command line, so only allow a conservative character set.
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,99}$")
# npm cmd-shim line, e.g.  "%dp0%\node_modules\@openai\codex\bin\codex.js" %*
_SHIM_SCRIPT = re.compile(r'"%~?dp0%?\\([^"]+?\.(?:js|cjs|mjs))"', re.IGNORECASE)
_URL = re.compile(r"https://[^\s\"'<>]+")


def _patterns(*expressions):
	return tuple(re.compile(expression, re.IGNORECASE) for expression in expressions)


# All classification patterns use word boundaries, and numbers only count with HTTP context.
_NOT_INSTALLED_PATTERNS = _patterns(
	r"is not recognized as an internal or external command",
	r"\bnode: not found\b",
	r"\bcannot find module\b",
	r"the system cannot find the path specified",
)
_USAGE_LIMIT_PATTERNS = _patterns(
	r"\busage limit\b",
	r"\brate[ -]limit(?:ed|s)?\b",
	r"\bstatus(?: code)?:? 429\b",
	r"\b429 too many requests\b",
	r"\btoo many requests\b",
	r"\bquota\b",
)
_NOT_LOGGED_IN_PATTERNS = _patterns(
	r"\bnot (?:logged|signed) in\b",
	r"\b401 unauthori[sz]ed\b",
	r"\bstatus(?: code)?:? 401\b",
	r"\bunauthori[sz]ed\b",
	r"\b(?:sign|log) in\b",
	r"\blogin\b",
	r"\bmissing bearer\b",
	r"\b(?:token|session) (?:has )?expired\b",
	r"\brefresh token\b",
)
_NETWORK_PATTERNS = _patterns(
	r"\bstream disconnected\b",
	r"\berror sending request\b",
	r"\bconnection (?:refused|reset|closed|aborted|failed|error|timed out)\b",
	r"\bfailed to connect\b",
	r"\bdns\b",
	r"\bfailed to lookup address\b",
	r"\bname or service not known\b",
	r"\bno such host\b",
	r"\bnetwork is unreachable\b",
	r"\btls\b",
	r"\bssl\b",
	r"\bcertificate\b",
	r"\bhandshake\b",
	r"\btimed out\b",
)

# Structure of `codex exec` stderr: a header between "--------" lines, then sections that start
# with a marker line ("user", "thinking", "codex", "exec", "tokens used") followed by content.
_ERROR_LINE = re.compile(r"^(?:\[?\d{4}-\d\d-\d\d[T ][^\s\]]*\]?\s+)?(?:ERROR\b|Error:|error:)")
_HEADER_LINE = re.compile(
	r"^(?:OpenAI Codex\b|(?:workdir|model|provider|approval|sandbox|reasoning effort|"
	r"reasoning summaries|session id):)",
	re.IGNORECASE,
)
_SECTION_MARKERS = ("user", "thinking", "codex", "exec", "assistant", "reasoning", "tokens used")
TAIL_LINES = 3


class CodexError(Exception):
	"""A failure running Codex.
	@ivar kind: one of the KIND_* constants.
	@ivar message: an English, human readable message without technical detail.
	@ivar detail: technical diagnostic text (for example Codex error lines), may be empty.
	@ivar reason: one of the REASON_* constants or "", for more specific advice.
	"""

	def __init__(self, kind, message, detail="", reason=""):
		super().__init__(message)
		self.kind = kind
		self.message = message
		self.detail = detail or ""
		self.reason = reason or ""

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


def expandUserPath(path):
	"""Normalise the configured executable path: trim, drop quotes, expand %VARIABLES%."""
	return os.path.expandvars((path or "").strip().strip('"'))


# Working directory


def defaultWorkDir(env=None, isWindows=IS_WINDOWS):
	"""%LOCALAPPDATA%\\askGPT\\workdir on Windows, falling back to the temp directory."""
	env = os.environ if env is None else env
	localAppData = env.get("LOCALAPPDATA") if isWindows else None
	if localAppData:
		return os.path.join(localAppData, "askGPT", "workdir")
	return os.path.join(tempfile.gettempdir(), "askGPT-workdir")


def _removeFile(path, attempts=5, delay=0.2):
	"""Delete a file, retrying briefly because Windows may still hold it open. Returns True when gone."""
	for attempt in range(attempts):
		try:
			os.remove(path)
			return True
		except FileNotFoundError:
			return True
		except OSError:
			if attempt + 1 < attempts:
				time.sleep(delay)
	return False


def cleanStaleAnswerFiles(workDir):
	"""Delete answer files left behind by an earlier crash. Returns the number removed."""
	removed = 0
	pattern = os.path.join(glob.escape(workDir), ANSWER_FILE_PREFIX + "*" + ANSWER_FILE_SUFFIX)
	for path in glob.glob(pattern):
		if _removeFile(path, attempts=1):
			removed += 1
	return removed


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
			paths.append(ntpath.join(appData, "npm", "codex.cmd"))
			paths.append(ntpath.join(appData, "npm", "codex.exe"))
		if localAppData:
			paths.append(ntpath.join(localAppData, "Programs", "codex", "codex.exe"))
			paths.append(ntpath.join(localAppData, "Programs", "OpenAI", "Codex", "codex.exe"))
			paths.append(ntpath.join(localAppData, "Microsoft", "WinGet", "Links", "codex.exe"))
		if userProfile:
			paths.append(ntpath.join(userProfile, ".local", "bin", "codex.exe"))
			paths.append(ntpath.join(userProfile, "scoop", "shims", "codex.exe"))
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
	userPath = expandUserPath(userPath)
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


def findNode(baseDir, env=None, which=shutil.which, isFile=os.path.isfile):
	"""node.exe next to the shim, on PATH, or in the default Node.js install folders."""
	env = os.environ if env is None else env
	local = ntpath.join(baseDir, "node.exe")
	if isFile(local):
		return local
	onPath = which("node")
	if onPath:
		return onPath
	for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
		root = env.get(variable)
		if root:
			candidate = ntpath.join(root, "nodejs", "node.exe")
			if isFile(candidate):
				return candidate
	return None


def resolveCmdShim(cmdPath, readText=_readText, which=shutil.which, isFile=os.path.isfile, env=None):
	"""Turn an npm .cmd shim into [node, script] so no cmd.exe is needed.
	Returns None for an unreadable or unrecognised shim (the caller then falls back to cmd.exe).
	@raise CodexError: notInstalled when the shim is recognised but its script or Node.js is missing.
	"""
	try:
		content = readText(cmdPath)
	except OSError:
		return None
	match = _SHIM_SCRIPT.search(content)
	if not match:
		return None
	baseDir = ntpath.dirname(cmdPath)
	script = ntpath.join(baseDir, *re.split(r"[\\/]", match.group(1)))
	if not isFile(script):
		raise CodexError(
			KIND_NOT_INSTALLED,
			"The Codex installation is incomplete. Run npm install -g @openai/codex again.",
			detail="Missing %s" % script,
			reason=REASON_INCOMPLETE_INSTALL,
		)
	node = findNode(baseDir, env=env, which=which, isFile=isFile)
	if not node:
		raise CodexError(
			KIND_NOT_INSTALLED,
			"Node.js was not found. Codex installed with npm needs Node.js: install it from nodejs.org "
			"or with winget install OpenJS.NodeJS.LTS, then restart NVDA.",
			detail="node.exe not found for %s" % cmdPath,
			reason=REASON_NODE_MISSING,
		)
	return [node, script]


def systemExecutable(name, env=None):
	"""Absolute path of a program in %SystemRoot%\\System32, so PATH cannot substitute another one."""
	env = os.environ if env is None else env
	root = env.get("SystemRoot") or env.get("windir") or "C:\\Windows"
	return ntpath.join(root, "System32", name)


_CMD_FORBIDDEN = ('"', "%", "\r", "\n")


def quoteForCmd(args):
	"""Quote every argument for cmd.exe. Inside quotes & | < > ^ are literal; quotes, percent signs and
	line breaks cannot be made safe, so they are refused. Only fixed arguments and paths ever get here.
	"""
	quoted = []
	for arg in args:
		if any(ch in arg for ch in _CMD_FORBIDDEN) or arg.endswith("\\"):
			raise CodexError(
				KIND_CONFIG,
				"A path contains characters that cannot be passed safely to cmd.exe: %s" % arg,
			)
		quoted.append('"%s"' % arg)
	return " ".join(quoted)


def buildCommand(exePath, args, isWindows=IS_WINDOWS, resolveShim=resolveCmdShim, env=None):
	"""Build the argv (or, for an unrecognised .cmd/.bat on Windows, a cmd.exe command line string)."""
	args = list(args)
	if isWindows and exePath.lower().endswith((".cmd", ".bat")):
		resolved = resolveShim(exePath)
		if resolved:
			return resolved + args
		inner = quoteForCmd([exePath] + args)
		# /s strips only the outermost quotes, leaving every argument individually quoted.
		# /v:off keeps "!" literal, /d skips AutoRun commands.
		return '"%s" /d /v:off /s /c "%s"' % (systemExecutable("cmd.exe", env), inner)
	return [exePath] + args


def buildExecArgs(outputFile, model="", reasoningEffort=""):
	"""Fixed codex exec arguments. The prompt is never on the command line; "-" reads it from stdin.
	-c values are passed without quotes: Codex parses the value as TOML and, when that fails,
	uses the raw text as a string (verified with codex-cli 0.160.0).
	"""
	args = [
		"exec",
		"--skip-git-repo-check",
		"--sandbox",
		"read-only",
		"--color",
		"never",
		"-c",
		"approval_policy=never",
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
		args += ["-c", "model_reasoning_effort=%s" % reasoningEffort]
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


def diagnosticLines(stderr, prompt=""):
	"""Pick the lines of `codex exec` stderr that describe a failure.
	Returns (errorLines, tailLines): every ERROR/Error: line, plus the last few lines that are not
	part of the header, the echoed prompt, or a user/thinking/codex/exec section (model text).
	"""
	promptLines = {line.strip() for line in (prompt or "").splitlines() if line.strip()}
	errorLines = []
	plainLines = []
	inHeader = False
	inSection = False
	for rawLine in (stderr or "").splitlines():
		line = rawLine.strip()
		if not line:
			continue
		if _ERROR_LINE.match(line):
			errorLines.append(line)
			inSection = False
			continue
		if line in promptLines:
			continue
		if len(line) >= 4 and set(line) == {"-"}:
			inHeader = not inHeader
			continue
		if inHeader or _HEADER_LINE.match(line):
			continue
		lower = line.lower()
		if lower in _SECTION_MARKERS or lower.startswith("tokens used"):
			inSection = True
			continue
		if inSection or line.startswith("**"):
			# Model output, command output or a reasoning summary heading.
			continue
		plainLines.append(line)
	return errorLines, plainLines[-TAIL_LINES:]


def _tail(text, limit=300):
	text = (text or "").strip()
	return text[-limit:] if len(text) > limit else text


def classifyFailure(returnCode, stdout, stderr, prompt=""):
	"""Map a failed Codex run to a CodexError.
	The matching is heuristic (Codex has no stable error codes) and only looks at Codex's error lines
	and the end of stderr, never at the user's question or the model's text.
	"""
	errorLines, tailLines = diagnosticLines(stderr, prompt)
	stdoutErrors, _stdoutTail = diagnosticLines(stdout, prompt)
	# Error lines go last so the 300 character detail keeps them.
	relevant = tailLines + errorLines + stdoutErrors
	text = "\n".join(relevant)
	detail = _tail(text)

	def matches(patterns):
		return any(pattern.search(text) for pattern in patterns)

	if matches(_NOT_INSTALLED_PATTERNS):
		return CodexError(KIND_NOT_INSTALLED, "Codex could not be started. " + INSTALL_HELP, detail)
	if matches(_USAGE_LIMIT_PATTERNS):
		return CodexError(
			KIND_USAGE_LIMIT,
			"You have hit the usage limit of your ChatGPT plan. Please try again later.",
			detail,
		)
	if matches(_NOT_LOGGED_IN_PATTERNS):
		return CodexError(
			KIND_NOT_LOGGED_IN,
			"Codex is not signed in to ChatGPT. Use Sign in with ChatGPT in the Ask GPT settings.",
			detail,
		)
	if matches(_NETWORK_PATTERNS):
		return CodexError(
			KIND_NETWORK,
			"Couldn't reach ChatGPT. Check your internet connection and try again.",
			detail,
		)
	return CodexError(KIND_OTHER, "Codex could not answer (exit code %s)." % returnCode, detail)


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


def extractUrl(text):
	"""First https:// address in text, without trailing punctuation, or None."""
	match = _URL.search(text or "")
	return match.group(0).rstrip(".,;:)]}") if match else None


# Windows Job Objects: every process in the job dies when the job is terminated or its handle closes,
# even if NVDA itself crashes. Structures are declared with pointer-sized fields for 32 and 64 bit.

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class IO_COUNTERS(ctypes.Structure):
	_fields_ = [
		("ReadOperationCount", ctypes.c_uint64),
		("WriteOperationCount", ctypes.c_uint64),
		("OtherOperationCount", ctypes.c_uint64),
		("ReadTransferCount", ctypes.c_uint64),
		("WriteTransferCount", ctypes.c_uint64),
		("OtherTransferCount", ctypes.c_uint64),
	]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
	_fields_ = [
		("PerProcessUserTimeLimit", ctypes.c_int64),  # LARGE_INTEGER
		("PerJobUserTimeLimit", ctypes.c_int64),  # LARGE_INTEGER
		("LimitFlags", ctypes.c_uint32),  # DWORD
		("MinimumWorkingSetSize", ctypes.c_size_t),  # SIZE_T
		("MaximumWorkingSetSize", ctypes.c_size_t),  # SIZE_T
		("ActiveProcessLimit", ctypes.c_uint32),  # DWORD
		("Affinity", ctypes.c_size_t),  # ULONG_PTR
		("PriorityClass", ctypes.c_uint32),  # DWORD
		("SchedulingClass", ctypes.c_uint32),  # DWORD
	]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
	_fields_ = [
		("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
		("IoInfo", IO_COUNTERS),
		("ProcessMemoryLimit", ctypes.c_size_t),
		("JobMemoryLimit", ctypes.c_size_t),
		("PeakProcessMemoryUsed", ctypes.c_size_t),
		("PeakJobMemoryUsed", ctypes.c_size_t),
	]


_kernel32 = None


def _loadKernel32():
	"""A private kernel32 instance with full prototypes (never touches ctypes.windll's shared one)."""
	global _kernel32
	if _kernel32 is None:
		from ctypes import wintypes

		kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
		kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
		kernel32.CreateJobObjectW.restype = wintypes.HANDLE
		kernel32.SetInformationJobObject.argtypes = (
			wintypes.HANDLE,
			ctypes.c_int,
			ctypes.POINTER(JOBOBJECT_EXTENDED_LIMIT_INFORMATION),
			wintypes.DWORD,
		)
		kernel32.SetInformationJobObject.restype = wintypes.BOOL
		kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
		kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
		kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
		kernel32.TerminateJobObject.restype = wintypes.BOOL
		kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
		kernel32.CloseHandle.restype = wintypes.BOOL
		_kernel32 = kernel32
	return _kernel32


def _lastError():
	getter = getattr(ctypes, "get_last_error", None)  # Only exists on Windows.
	return getter() if getter else 0


class WindowsJob:
	"""A kill-on-close Job Object holding one child process and its descendants."""

	def __init__(self, kernel32=None):
		self._kernel32 = kernel32 or _loadKernel32()
		self.handle = self._kernel32.CreateJobObjectW(None, None)
		if not self.handle:
			raise OSError(_lastError(), "CreateJobObjectW failed")
		info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
		info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
		if not self._kernel32.SetInformationJobObject(
			self.handle,
			JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
			ctypes.byref(info),
			ctypes.sizeof(info),
		):
			error = _lastError()
			self.close()
			raise OSError(error, "SetInformationJobObject failed")

	def assign(self, proc):
		"""Put a subprocess.Popen in the job. Returns False on failure."""
		processHandle = getattr(proc, "_handle", None)
		if not self.handle or processHandle is None:
			return False
		return bool(self._kernel32.AssignProcessToJobObject(self.handle, int(processHandle)))

	def terminate(self):
		"""Kill every process in the job. Returns False on failure."""
		return bool(self.handle) and bool(self._kernel32.TerminateJobObject(self.handle, 1))

	def close(self):
		if self.handle:
			self._kernel32.CloseHandle(self.handle)
			self.handle = None


# Process handling


def _decode(data):
	if not data:
		return ""
	return data.decode("utf-8", errors="replace")


def killProcessTree(proc, isWindows=IS_WINDOWS, run=None, job=None, killpg=None, env=None):
	"""Kill a process and its children synchronously. Never raises.
	Order: the Job Object (Windows), else taskkill /T (Windows) or the process group (POSIX), then the process.
	"""
	if proc is None:
		return
	if job is not None:
		try:
			if job.terminate():
				_reap(proc)
				return
		except Exception:
			pass
	try:
		if proc.poll() is not None:
			return
	except Exception:
		pass
	if isWindows:
		try:
			(run or subprocess.run)(
				[systemExecutable("taskkill.exe", env), "/T", "/F", "/PID", str(proc.pid)],
				stdin=subprocess.DEVNULL,
				stdout=subprocess.DEVNULL,
				stderr=subprocess.DEVNULL,
				creationflags=CREATE_NO_WINDOW,
				timeout=10,
			)
		except Exception:
			pass
	elif killpg is not None:
		try:
			# The child was started with start_new_session=True, so its process group id is its pid.
			killpg(proc.pid, SIGKILL)
		except Exception:
			pass
	try:
		proc.kill()
	except Exception:
		pass
	_reap(proc)


def _reap(proc, timeout=5):
	try:
		proc.wait(timeout=timeout)
	except Exception:
		pass


class CodexClient:
	"""Runs Codex as a child process. ask/loginStatus/logout/version block, so call them from a
	background thread. terminate() may be called from any thread to kill everything at once.
	"""

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
		jobFactory=None,
		killpg=None,
		env=None,
	):
		self.executablePath = (executablePath or "").strip()
		self.workDir = workDir or defaultWorkDir(isWindows=isWindows)
		self.model = (model or "").strip()
		self.reasoningEffort = (reasoningEffort or "").strip().lower()
		self.timeout = clampTimeout(timeout)
		self._popen = popen
		self._isWindows = isWindows
		self._finder = finder
		self._env = os.environ if env is None else env
		if jobFactory is None and isWindows:
			jobFactory = WindowsJob
		self._jobFactory = jobFactory
		if killpg is None and not isWindows:
			killpg = getattr(os, "killpg", None)
		self._killpg = killpg
		self._lock = threading.Lock()
		self._process = None
		self._job = None
		self._killedProcess = None  # The run process that terminate() killed.
		self._loginProcess = None
		self._loginJob = None
		self.loginUrl = None

	# Helpers

	def locate(self):
		"""Return the Codex executable path or raise CodexError(notInstalled)."""
		path = self._finder(self.executablePath)
		if not path:
			if self.executablePath:
				raise CodexError(
					KIND_NOT_INSTALLED,
					"The Codex executable path in the settings does not exist: %s" % expandUserPath(self.executablePath),
					detail=expandUserPath(self.executablePath),
					reason=REASON_CUSTOM_PATH,
				)
			raise CodexError(KIND_NOT_INSTALLED, "The OpenAI Codex CLI was not found. " + INSTALL_HELP)
		return path

	def _command(self, args):
		return buildCommand(self.locate(), args, isWindows=self._isWindows, env=self._env)

	def _baseKwargs(self, cwd):
		kwargs = {"cwd": cwd}
		if self._isWindows:
			kwargs["creationflags"] = CREATE_NO_WINDOW
		else:
			kwargs["start_new_session"] = True
		env = dict(self._env)
		env["NO_COLOR"] = "1"
		kwargs["env"] = env
		return kwargs

	def _ensureWorkDir(self):
		try:
			os.makedirs(self.workDir, exist_ok=True)
		except OSError as e:
			raise CodexError(KIND_OTHER, "Could not create the working directory %s: %s" % (self.workDir, e))
		return self.workDir

	def _launch(self, command, kwargs):
		"""Start a process and, on Windows, put it in a kill-on-close Job Object. Returns (proc, job)."""
		try:
			proc = self._popen(command, **kwargs)
		except FileNotFoundError:
			raise CodexError(KIND_NOT_INSTALLED, "The OpenAI Codex CLI could not be started. " + INSTALL_HELP)
		except OSError as e:
			raise CodexError(KIND_OTHER, "Codex could not be started.", detail=str(e))
		job = None
		if self._jobFactory is not None:
			try:
				job = self._jobFactory()
				if not job.assign(proc):
					job.close()
					job = None
			except Exception:
				job = None  # killProcessTree falls back to taskkill.
		return proc, job

	def _kill(self, proc, job):
		killProcessTree(proc, self._isWindows, job=job, killpg=self._killpg, env=self._env)

	def _run(self, args, inputText=None, timeout=None, cancelEvent=None):
		"""Run codex with args. Returns (returnCode, stdoutText, stderrText)."""
		command = self._command(args)
		timeout = self.timeout if timeout is None else timeout
		kwargs = self._baseKwargs(self._ensureWorkDir())
		kwargs.update(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		proc, job = self._launch(command, kwargs)
		with self._lock:
			self._process, self._job = proc, job
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
					self._kill(proc, job)
					break
		finally:
			self._cleanUp(proc, threads, readers)
			with self._lock:
				if self._process is proc:
					self._process, self._job = None, None
				killedByTerminate = self._killedProcess is proc
				if killedByTerminate:
					self._killedProcess = None
			if job is not None:
				job.close()  # Kill-on-close also ends any process that outlived codex.
		if failure is None and (killedByTerminate or (cancelEvent is not None and cancelEvent.is_set())):
			# Killed by terminate() (NVDA exiting) or cancelled while it was exiting: its exit code means nothing.
			failure = CodexError(KIND_CANCELLED, "The request was cancelled.")
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
		_reap(proc)

	# Public API

	def ask(self, prompt, cancelEvent=None):
		"""Send prompt (already built with buildPrompt) and return the answer text."""
		self._ensureWorkDir()
		fd, outputFile = tempfile.mkstemp(prefix=ANSWER_FILE_PREFIX, suffix=ANSWER_FILE_SUFFIX, dir=self.workDir)
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
				# Codex sometimes exits 0 after an error; report that error rather than "empty".
				error = classifyFailure(returnCode, stdout, stderr, prompt)
				if error.kind != KIND_OTHER:
					raise error
				raise CodexError(KIND_OTHER, "Codex returned an empty answer.", error.detail, REASON_EMPTY_ANSWER)
			return answer
		finally:
			# The process has been reaped by now; retry because Windows may release the file late.
			_removeFile(outputFile)

	def loginStatus(self):
		"""Return (loggedIn, text) from `codex login status`."""
		returnCode, stdout, stderr = self._run(["login", "status"], timeout=30)
		return parseLoginStatus(returnCode, "%s\n%s" % (stdout, stderr))

	def login(self, onUrl=None):
		"""Start `codex login` without waiting. It opens the default browser for ChatGPT sign-in.
		Its output is read in a background thread; the first https:// address (the sign-in link,
		which Codex prints on stderr) is stored in loginUrl and passed to onUrl(url).
		Returns the process so the caller can wait for it in a background thread.
		"""
		self.cancelLogin()
		self.loginUrl = None
		command = self._command(["login"])
		kwargs = self._baseKwargs(self._ensureWorkDir())
		kwargs.update(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
		if self._isWindows:
			kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
		proc, job = self._launch(command, kwargs)
		with self._lock:
			self._loginProcess, self._loginJob = proc, job

		def reader():
			try:
				for rawLine in iter(proc.stdout.readline, b""):
					if self.loginUrl is None:
						url = extractUrl(_decode(rawLine))
						if url:
							self.loginUrl = url
							if onUrl is not None:
								try:
									onUrl(url)
								except Exception:
									pass
			except Exception:
				pass
			finally:
				try:
					proc.stdout.close()
				except Exception:
					pass

		threading.Thread(target=reader, name="askGPT.loginOutput", daemon=True).start()
		return proc

	def cancelLogin(self):
		"""Kill a running `codex login` (synchronously) and release its job. Safe to call repeatedly."""
		with self._lock:
			proc, job = self._loginProcess, self._loginJob
			self._loginProcess, self._loginJob = None, None
		if proc is not None:
			self._kill(proc, job)
		if job is not None:
			job.close()

	def terminate(self):
		"""Kill the running request and any login, synchronously. Used when NVDA exits or the add-on unloads."""
		with self._lock:
			proc, job = self._process, self._job
			self._killedProcess = proc
		if proc is not None:
			self._kill(proc, job)
		self.cancelLogin()

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
