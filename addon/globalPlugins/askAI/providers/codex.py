# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: ChatGPT through the official OpenAI Codex CLI (`codex exec`), signed in with a
# ChatGPT account. Pure standard library, no NVDA imports.

import ntpath
import os
import re
import tempfile

from . import modelCatalog
from . import base
from .base import (
	KIND_NOT_INSTALLED,
	KIND_NOT_LOGGED_IN,
	KIND_USAGE_LIMIT,
	KIND_NETWORK,
	KIND_OTHER,
	KIND_CONFIG,
	REASON_EMPTY_ANSWER,
	STATE_API_KEY,
	STATE_SIGNED_OUT,
	STATE_SUBSCRIPTION,
	ProviderError,
	patterns,
)

PROVIDER_ID = "codex"

# Offered when Codex's model list is unavailable. Models may support more (for example xhigh).
REASONING_EFFORTS = ("low", "medium", "high")

PROJECT_INSTRUCTIONS = (
	"The current working directory is the user's project folder. If the user asks you to create a "
	"document or file (for example Word, PDF, text, spreadsheet, HTML), save it in this folder, then tell "
	"the user the exact file name. Do not modify or delete existing files unless the user asks."
)
PROJECT_READ_ONLY_INSTRUCTIONS = (
	"The current working directory is the user's project folder, but you have no tools to open or change "
	"files in it. If the user asks about its files, ask them to paste or attach the text. If the user asks "
	"for a file, give the content in your answer; they can save it with Save answer as."
)

INSTALL_HELP = (
	"Install Node.js, then run npm install -g @openai/codex in a command prompt, "
	"or download codex from github.com/openai/codex/releases. "
	"Then restart NVDA, or set the Codex executable path in the Ask AI settings."
)

# codex-cli 0.160.0 accepts these instead of the ChatGPT sign-in ("Run codex login or provide an API key
# through a supported auth env var"); CODEX_API_KEY in particular overrides the sign-in for `codex exec`.
# Removing them keeps usage on the user's ChatGPT plan instead of per-use API billing. OPENAI_BASE_URL and
# the other OPENAI_* settings could send requests to another host, so the whole prefix is removed.
STRIPPED_ENVIRONMENT = ("OPENAI_*", "CODEX_API_KEY")

_USAGE_LIMIT_PATTERNS = patterns(
	r"\busage limit\b",
	r"\brate[ -]limit(?:ed|s)?\b",
	r"\bstatus(?: code)?:? 429\b",
	r"\b429 too many requests\b",
	r"\btoo many requests\b",
	r"\bquota\b",
)
_NOT_LOGGED_IN_PATTERNS = patterns(
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
_NETWORK_PATTERNS = patterns(
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

# Always passed (verified with codex-cli 0.160.0 `codex exec --help` and `codex features list`):
# --ignore-user-config  "Do not load $CODEX_HOME/config.toml; auth still uses CODEX_HOME"
# --ignore-rules        "Do not load user or project execpolicy .rules files"
# --ephemeral           "Run without persisting session files to disk"
# --disable <FEATURE>   "Equivalent to -c features.<name>=false" for features that reach outside the request
#                       (connectors/apps, plugins, browser and computer use, sub-agents, image generation, hooks).
# -c web_search=disabled  turns off the web_search tool (an enum: Codex refuses to start on an unknown value).
DISABLED_FEATURES = ("apps", "plugins", "browser_use", "computer_use", "multi_agent", "image_generation", "hooks")
HARDENING_ARGS = ["--ignore-user-config", "--ignore-rules", "--ephemeral"]
for _feature in DISABLED_FEATURES:
	HARDENING_ARGS += ["--disable", _feature]
HARDENING_ARGS += ["-c", "web_search=disabled"]
# Without a writable project folder Codex gets no shell. ("unified_exec" is accepted by 0.160.0 but still
# listed as enabled by `codex features list`; shell_tool is reported disabled.)
NO_SHELL_ARGS = ["--disable", "shell_tool", "--disable", "unified_exec"]

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


def candidatePaths(env=None, isWindows=base.IS_WINDOWS):
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


def findCodex(userPath="", env=None, which=base.shutil.which, isFile=os.path.isfile, isWindows=base.IS_WINDOWS):
	"""Return the path of the Codex executable, or None.
	A configured path wins; then PATH; then well known locations.
	"""
	return base.findExecutable("codex", candidatePaths(env, isWindows), userPath, which, isFile, isWindows)


def buildExecArgs(
	outputFile,
	model="",
	reasoningEffort="",
	projectFolder="",
	allowWrites=False,
	isWindows=base.IS_WINDOWS,
	images=(),
):
	"""Fixed codex exec arguments. The prompt is never on the command line; "-" reads it from stdin.
	-c values are passed without quotes: Codex parses the value as TOML and, when that fails,
	uses the raw text as a string (verified with codex-cli 0.160.0).
	With a project folder, Codex works in it (-C). With allowWrites it may create and edit files there
	(workspace-write), with network access explicitly off and /tmp and $TMPDIR not writable; otherwise
	it stays read-only. The approval bypass flag is never used.
	images (paths in the add-on's private request folder) are passed as --image=<path>
	(`codex exec --help`: "-i, --image <FILE>...  Optional image(s) to attach to the initial prompt").
	The option takes several values, so the images come before -o, which ends any list.
	"""
	sandbox = "workspace-write" if projectFolder and allowWrites else "read-only"
	args = [
		"exec",
		"--skip-git-repo-check",
		"--sandbox",
		sandbox,
		"--color",
		"never",
		"-c",
		"approval_policy=never",
	]
	args += HARDENING_ARGS
	if sandbox != "workspace-write":
		# No shell at all unless the user allowed writing in a project folder (Codex's sandbox then applies).
		args += NO_SHELL_ARGS
	if projectFolder:
		args += ["-C", base.projectFolderArgument(projectFolder, isWindows)]
	if sandbox == "workspace-write":
		args += [
			"-c",
			"sandbox_workspace_write.network_access=false",
			"-c",
			"sandbox_workspace_write.exclude_tmpdir_env_var=true",
			"-c",
			"sandbox_workspace_write.exclude_slash_tmp=true",
		]
	for image in images:
		args.append("--image=%s" % image)
	args += ["-o", outputFile]
	model = (model or "").strip()
	if model:
		if not base.isValidModel(model):
			raise ProviderError(KIND_CONFIG, "The model name contains characters that are not allowed: %s" % model, provider=PROVIDER_ID)
		args += ["-m", model]
	reasoningEffort = (reasoningEffort or "").strip().lower()
	if reasoningEffort:
		if not base.isValidEffort(reasoningEffort):
			raise ProviderError(KIND_CONFIG, "Unknown reasoning effort: %s" % reasoningEffort, provider=PROVIDER_ID)
		args += ["-c", "model_reasoning_effort=%s" % reasoningEffort]
	args.append("-")
	return args


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


def classifyFailure(returnCode, stdout, stderr, prompt=""):
	"""Map a failed Codex run to a ProviderError.
	The matching is heuristic (Codex has no stable error codes) and only looks at Codex's error lines
	and the end of stderr, never at the user's question or the model's text.
	"""
	errorLines, tailLines = diagnosticLines(stderr, prompt)
	stdoutErrors, _stdoutTail = diagnosticLines(stdout, prompt)
	# Error lines go last so the 300 character detail keeps them.
	relevant = tailLines + errorLines + stdoutErrors
	text = "\n".join(relevant)
	detail = base.tail(text)

	def matches(expressions):
		return any(pattern.search(text) for pattern in expressions)

	def error(kind, message):
		return ProviderError(kind, message, detail, provider=PROVIDER_ID)

	if matches(base.NOT_INSTALLED_PATTERNS):
		return error(KIND_NOT_INSTALLED, "Codex could not be started. " + INSTALL_HELP)
	if matches(_USAGE_LIMIT_PATTERNS):
		return error(KIND_USAGE_LIMIT, "You have hit the usage limit of your ChatGPT plan. Please try again later.")
	if matches(_NOT_LOGGED_IN_PATTERNS):
		return error(KIND_NOT_LOGGED_IN, "Codex is not signed in to ChatGPT. Use Sign in to ChatGPT in the Ask AI settings.")
	if matches(_NETWORK_PATTERNS):
		return error(KIND_NETWORK, "Couldn't reach ChatGPT. Check your internet connection and try again.")
	return error(KIND_OTHER, "Codex could not answer (exit code %s)." % returnCode)


def parseLoginStatus(returnCode, text):
	"""Return (loggedIn, cleanedText) from `codex login status` output."""
	lines = [
		line.strip()
		for line in base.stripAnsi(text or "").splitlines()
		if line.strip() and not line.strip().upper().startswith("WARNING")
	]
	cleaned = "\n".join(lines)
	lower = cleaned.lower()
	loggedIn = returnCode == 0 and "logged in" in lower and "not logged in" not in lower
	return loggedIn, cleaned


def isApiKeyLogin(statusText):
	"""True if Codex is signed in with an API key (billed per use) instead of a ChatGPT account."""
	return "api key" in (statusText or "").lower()


def loginState(returnCode, text):
	"""(state, cleanedText) for `codex login status`."""
	loggedIn, cleaned = parseLoginStatus(returnCode, text)
	if not loggedIn:
		return STATE_SIGNED_OUT, cleaned
	return (STATE_API_KEY if isApiKeyLogin(cleaned) else STATE_SUBSCRIPTION), cleaned


class CodexProvider(base.CliProvider):
	providerId = PROVIDER_ID
	displayName = "ChatGPT (Codex)"
	shortName = "ChatGPT"
	programName = "Codex"
	executableName = "codex"
	npmPackage = "@openai/codex"
	installHelp = INSTALL_HELP
	strippedEnvironment = STRIPPED_ENVIRONMENT
	loginArgs = ("login",)
	logoutArgs = ("logout",)
	loginAcceptsCode = False
	defaultEfforts = REASONING_EFFORTS
	imagesAttachedDirectly = True
	# Without a shell Codex cannot open other files, so only text (in the prompt) and images are accepted.
	readsOtherFiles = False

	@classmethod
	def candidatePaths(cls, env=None, isWindows=base.IS_WINDOWS):
		return candidatePaths(env, isWindows)

	@classmethod
	def projectInstructions(cls, allowWrites=True):
		return PROJECT_INSTRUCTIONS if allowWrites else PROJECT_READ_ONLY_INSTRUCTIONS

	def find(self, userPath=""):
		if self._finder is not None:
			return self._finder(userPath)
		return findCodex(userPath, env=self._env, isWindows=self._isWindows)

	def models(self):
		"""Models from Codex's own cache (%CODEX_HOME%\\models_cache.json)."""
		return modelCatalog.loadModels(env=self._env)

	def classifyFailure(self, returnCode, stdout, stderr, prompt=""):
		return classifyFailure(returnCode, stdout, stderr, prompt)

	def ask(self, prompt, cancelEvent=None, projectFolder=None, allowWrites=None, model=None, effort=None, attachments=None):
		"""Send prompt (already built with buildPrompt) and return the answer text."""
		projectFolder, allowWrites, model, effort = self._requestSettings(projectFolder, allowWrites, model, effort)
		self._checkProjectFolder(projectFolder)
		self._ensureWorkDir()
		# The answer file stays in the private working folder, never in the project.
		fd, outputFile = tempfile.mkstemp(prefix=base.ANSWER_FILE_PREFIX, suffix=base.ANSWER_FILE_SUFFIX, dir=self.workDir)
		os.close(fd)
		try:
			args = buildExecArgs(
				outputFile,
				model,
				effort,
				projectFolder=projectFolder,
				allowWrites=allowWrites,
				isWindows=self._isWindows,
				images=attachments.imagePaths() if attachments is not None else (),
			)
			returnCode, stdout, stderr = self._run(
				args,
				inputText=prompt,
				cancelEvent=cancelEvent,
				cwd=projectFolder or None,
			)
			if returnCode != 0:
				raise classifyFailure(returnCode, stdout, stderr, prompt)
			try:
				answer = base.readText(outputFile).strip()
			except OSError:
				answer = ""
			if not answer:
				answer = stdout.strip()
			if not answer:
				# Codex sometimes exits 0 after an error; report that error rather than "empty".
				error = classifyFailure(returnCode, stdout, stderr, prompt)
				if error.kind != KIND_OTHER:
					raise error
				raise ProviderError(KIND_OTHER, "Codex returned an empty answer.", error.detail, REASON_EMPTY_ANSWER, provider=PROVIDER_ID)
			self.lastMeta = {}
			return answer
		finally:
			# The process has been reaped by now; retry because Windows may release the file late.
			base.removeFile(outputFile)

	def loginStatus(self):
		"""Return (state, text) from `codex login status`."""
		returnCode, stdout, stderr = self._run(["login", "status"], timeout=30)
		return loginState(returnCode, "%s\n%s" % (stdout, stderr))


# Backward compatible names.
CodexClient = CodexProvider
CodexError = ProviderError
