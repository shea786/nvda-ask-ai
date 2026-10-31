# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: Claude through the official Claude Code CLI (`claude -p`), signed in with a
# Claude subscription (Pro, Max, Team or Enterprise). Pure standard library, no NVDA imports.
#
# Verified against Claude Code 2.1.314 (`claude --help`, `claude auth ... --help`) and
# docs.claude.com/en/docs/claude-code (cli-reference, headless, permissions, setup, model-config):
# * `-p` with no prompt argument reads the prompt from stdin ("Non-interactive mode reads stdin").
# * `--output-format json` prints one JSON object: result, is_error, subtype, session_id, usage, ...
# * `--tools ""` disables all built-in tools; `--tools Read,Glob,Grep` allows only those. --tools does
#   not affect MCP tools, so `--strict-mcp-config` (no --mcp-config) loads no MCP servers and
#   `--disallowedTools mcp__*` denies any that remain.
# * `--restricted` (2.1.248+) removes the command and code running tools and WebFetch unless --tools
#   names them, confines the file tools to the working directory, ignores user, project and local
#   settings files (so a project folder's .claude/settings.json hooks never run) and refuses bypassPermissions.
# * `--permission-prompts none` (2.1.259+): anything that would ask a person is denied.
# * `--permission-mode acceptEdits` auto-accepts file edits in the working directory; `dontAsk` denies
#   everything that would otherwise prompt. bypassPermissions / --dangerously-skip-permissions are never used.

import datetime
import json
import ntpath
import os
import re

from . import base, modelCatalog
from .base import (
	KIND_CONFIG,
	KIND_NETWORK,
	KIND_NOT_INSTALLED,
	KIND_NOT_LOGGED_IN,
	KIND_OTHER,
	KIND_USAGE_LIMIT,
	REASON_BUSY,
	REASON_EMPTY_ANSWER,
	REASON_OUTDATED,
	STATE_API_KEY,
	STATE_SIGNED_OUT,
	STATE_SUBSCRIPTION,
	ProviderError,
	patterns,
)

PROVIDER_ID = "claude"

# `claude --help`: "--effort <level>  Effort level for the current session (low, medium, high, xhigh, max)".
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Model aliases from the Claude Code docs (model-config, "Model aliases"); any full model name can be typed.
MODEL_ALIASES = (
	("sonnet", "Sonnet"),
	("opus", "Opus"),
	("haiku", "Haiku"),
	("fable", "Fable"),
)

READ_TOOLS = ("Read", "Glob", "Grep")
WRITE_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep")

PROJECT_INSTRUCTIONS = (
	"The current working directory is the user's project folder. You can read the files in it and "
	"create or edit text-based files there (for example .txt, .md, .html, .csv). You cannot run commands "
	"or programs, so you cannot make Word, PDF or other binary files: for those, write the content as "
	"Markdown or HTML, or answer in text and tell the user they can save it as Word or PDF with "
	"Save answer as in Ask AI. Save requested files in this folder, then tell the user the exact file name. "
	"Do not modify or delete existing files unless the user asks."
)
PROJECT_READ_ONLY_INSTRUCTIONS = (
	"The current working directory is the user's project folder. You may read the files in it, but you "
	"cannot create or change files there. If the user asks for a file, give the content in your answer; "
	"they can save it with Save answer as in Ask AI."
)

INSTALL_HELP = (
	"Install Claude Code: in PowerShell run irm https://claude.ai/install.ps1 | iex, "
	"or run npm install -g @anthropic-ai/claude-code in a command prompt. "
	"Then restart NVDA, or set the Claude Code executable path in the Ask AI settings."
)

# With either of these set, Claude Code would use (and bill) the Anthropic API instead of the user's
# Claude subscription, so they are never passed on.
STRIPPED_ENVIRONMENT = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

_NOT_LOGGED_IN_PATTERNS = patterns(
	r"\bnot logged in\b",
	r"please run /login",
	r"\binvalid api key\b",
	r"\binvalid auth token\b",
	r"\boauth token (?:has )?(?:expired|revoked)\b",
	r"\blogin expired\b",
	r"\bauthentication required\b",
	r"\bauthentication_error\b",
	r"\b401\b",
)
# Server load, not the user's limit ("Server is temporarily limiting requests (not your usage limit)").
_BUSY_PATTERNS = patterns(
	r"\bnot your usage limit\b",
	r"\boverloaded\b",
	r"\bhigh load\b",
	r"\btemporarily limiting requests\b",
	r"\b529\b",
)
_USAGE_LIMIT_PATTERNS = patterns(
	r"\busage limit\b",
	r"\byou've hit your\b",
	r"\byou’ve hit your\b",
	r"\byou've reached your\b.*\blimit\b",
	r"\bout of usage credits\b",
	r"\bspend limit\b",
	r"\bcredit balance is too low\b",
	r"\brate[ _-]limit",
	r"\b429\b",
)
_NETWORK_PATTERNS = patterns(
	r"\bconnection error\b",
	r"\brequest timed out\b",
	r"\beconn(?:refused|reset|aborted)\b",
	r"\benotfound\b",
	r"\betimedout\b",
	r"\beai_again\b",
	r"\bgetaddrinfo\b",
	r"\bfetch failed\b",
	r"\bsocket hang up\b",
	r"\bunable to connect\b",
	r"\bnetwork\b",
	r"\bcertificate\b",
	r"\bssl\b",
)
_OUTDATED_PATTERNS = patterns(
	r"\bunknown option\b",
	r"\bunknown command\b",
	r"\bunrecognized option\b",
)
# Older Claude Code: "Claude AI usage limit reached|1767225600" (Unix time, seconds or milliseconds).
_RESET_EPOCH = re.compile(r"\|\s*(\d{9,13})\b")
# Current Claude Code: "You've hit your session limit · resets 3pm (Europe/London)".
_RESET_TEXT = re.compile(r"\bresets\s+([^·|\n]+)", re.IGNORECASE)


def candidatePaths(env=None, isWindows=base.IS_WINDOWS):
	"""Well known install locations, most likely first."""
	env = os.environ if env is None else env
	paths = []
	if isWindows:
		userProfile = env.get("USERPROFILE")
		appData = env.get("APPDATA")
		localAppData = env.get("LOCALAPPDATA")
		if userProfile:
			# The native installer (irm https://claude.ai/install.ps1 | iex).
			paths.append(ntpath.join(userProfile, ".local", "bin", "claude.exe"))
		if appData:
			# npm install -g @anthropic-ai/claude-code
			paths.append(ntpath.join(appData, "npm", "claude.cmd"))
		if localAppData:
			# winget install Anthropic.ClaudeCode
			paths.append(ntpath.join(localAppData, "Microsoft", "WinGet", "Links", "claude.exe"))
	else:
		home = env.get("HOME")
		if home:
			paths.append(os.path.join(home, ".local", "bin", "claude"))
			paths.append(os.path.join(home, ".npm-global", "bin", "claude"))
		paths.append("/usr/local/bin/claude")
	return paths


def findClaude(userPath="", env=None, which=base.shutil.which, isFile=os.path.isfile, isWindows=base.IS_WINDOWS):
	return base.findExecutable("claude", candidatePaths(env, isWindows), userPath, which, isFile, isWindows)


def toolsFor(projectFolder, allowWrites, readableFolder=""):
	"""Built-in tools Claude may use: none without a project folder, read tools for a read-only folder,
	read and file-writing tools when writing is allowed. Never Bash, PowerShell, WebFetch, WebSearch or Task.
	With attached files (readableFolder) and no project folder, only Read, so Claude can open them;
	Read returns images as pictures Claude can see (Claude Code docs, tools reference, Read).
	"""
	if not projectFolder:
		return ("Read",) if readableFolder else ()
	return WRITE_TOOLS if allowWrites else READ_TOOLS


def buildAskArgs(model="", effort="", projectFolder="", allowWrites=False, readableFolder=""):
	"""Fixed `claude -p` arguments. The prompt is never on the command line: it is read from stdin.
	Every list-valued option (--add-dir, --tools, --disallowedTools) is followed by another option, so it can
	never swallow a following argument.
	readableFolder is the add-on's private request folder with attached files. Without a project folder it is
	the working directory; with one it is added with --add-dir ("Additional directories to allow tool access
	to"), which --restricted includes in the folders the file tools are confined to.
	"""
	tools = toolsFor(projectFolder, allowWrites, readableFolder)
	writes = bool(projectFolder) and allowWrites
	args = [
		"-p",
		"--output-format",
		"json",
		"--no-session-persistence",
		"--strict-mcp-config",
		"--restricted",
	]
	if projectFolder and readableFolder:
		args += ["--add-dir", base.projectFolderArgument(readableFolder)]
	args += [
		"--tools",
		",".join(tools),
		"--disallowedTools",
		"mcp__*",
		"--permission-mode",
		"acceptEdits" if writes else "dontAsk",
		"--permission-prompts",
		"none",
	]
	model = (model or "").strip()
	if model:
		if not base.isValidModel(model):
			raise ProviderError(KIND_CONFIG, "The model name contains characters that are not allowed: %s" % model, provider=PROVIDER_ID)
		args += ["--model", model]
	effort = (effort or "").strip().lower()
	if effort:
		if not base.isValidEffort(effort):
			raise ProviderError(KIND_CONFIG, "Unknown reasoning effort: %s" % effort, provider=PROVIDER_ID)
		args += ["--effort", effort]
	return args


class ClaudeResult:
	"""The JSON object `claude -p --output-format json` prints."""

	def __init__(self, data):
		self.data = data
		result = data.get("result")
		self.text = result if isinstance(result, str) else ""
		self.isError = data.get("is_error") is True
		subtype = data.get("subtype")
		self.subtype = subtype if isinstance(subtype, str) else ""
		sessionId = data.get("session_id")
		self.sessionId = sessionId if isinstance(sessionId, str) else ""
		usage = data.get("usage")
		self.usage = usage if isinstance(usage, dict) else {}
		status = data.get("api_error_status")
		self.apiErrorStatus = status if isinstance(status, int) and not isinstance(status, bool) else None
		reason = data.get("terminal_reason")
		self.terminalReason = reason if isinstance(reason, str) else ""

	@property
	def failed(self):
		return self.isError or (bool(self.subtype) and self.subtype != "success")

	def meta(self):
		meta = {"sessionId": self.sessionId, "usage": self.usage}
		cost = self.data.get("total_cost_usd")
		if isinstance(cost, (int, float)) and not isinstance(cost, bool):
			meta["totalCostUsd"] = cost
		return meta


def parseResult(stdout):
	"""The result object from stdout, or None. Tolerates warning lines before the JSON."""
	text = (stdout or "").strip()
	if not text:
		return None
	candidates = [text] + [line.strip() for line in reversed(text.splitlines()) if line.strip().startswith("{")]
	for candidate in candidates:
		try:
			data = json.loads(candidate)
		except ValueError:
			continue
		if isinstance(data, dict) and (data.get("type") == "result" or "result" in data or "is_error" in data):
			return ClaudeResult(data)
	return None


def resetTime(text):
	"""(resetEpoch or None, resetText) for a usage limit message."""
	match = _RESET_EPOCH.search(text or "")
	if match:
		value = int(match.group(1))
		if value > 10 ** 11:
			value = value / 1000.0  # Milliseconds.
		return float(value), ""
	match = _RESET_TEXT.search(text or "")
	if match:
		return None, match.group(1).strip().rstrip(".")
	return None, ""


def formatResetTime(epoch, now=None):
	"""Local time for a reset epoch: "15:00" today, else "2026-11-03 09:00" (English-neutral)."""
	moment = datetime.datetime.fromtimestamp(epoch)
	today = datetime.datetime.fromtimestamp(now if now is not None else datetime.datetime.now().timestamp()).date()
	if moment.date() == today:
		return moment.strftime("%H:%M")
	return moment.strftime("%Y-%m-%d %H:%M")


def classifyFailure(returnCode, stdout, stderr, result=None, prompt=""):
	"""Map a failed `claude -p` run to a ProviderError. Only Claude Code's own result text (when the run
	failed), its API status and the end of stderr are inspected, never the user's question.
	"""
	if result is None:
		result = parseResult(stdout)
	parts = []
	stderrLines = [line.strip() for line in base.stripAnsi(stderr or "").splitlines() if line.strip()]
	promptLines = {line.strip() for line in (prompt or "").splitlines() if line.strip()}
	parts.extend(line for line in stderrLines[-4:] if line not in promptLines)
	if result is not None and result.text:
		parts.append(result.text.strip())
	elif result is None:
		stdoutLines = [line.strip() for line in base.stripAnsi(stdout or "").splitlines() if line.strip()]
		parts.extend(line for line in stdoutLines[-3:] if line not in promptLines)
	text = "\n".join(parts)
	status = result.apiErrorStatus if result is not None else None
	if status is not None:
		text += "\nstatus %d" % status
	detail = base.tail(text)

	def matches(expressions):
		return any(pattern.search(text) for pattern in expressions)

	def error(kind, message, reason="", **kwargs):
		return ProviderError(kind, message, detail, reason, provider=PROVIDER_ID, **kwargs)

	if matches(base.NOT_INSTALLED_PATTERNS):
		return error(KIND_NOT_INSTALLED, "Claude Code could not be started. " + INSTALL_HELP)
	if matches(_OUTDATED_PATTERNS):
		return error(
			KIND_CONFIG,
			"This Claude Code version is too old for Ask AI. Run claude update in a command prompt.",
			REASON_OUTDATED,
		)
	if status in (401, 403) or matches(_NOT_LOGGED_IN_PATTERNS):
		return error(KIND_NOT_LOGGED_IN, "Claude Code is not signed in. Use Sign in to Claude in the Ask AI settings.")
	if status in (500, 502, 503, 529) or matches(_BUSY_PATTERNS):
		return error(KIND_OTHER, "Claude is busy right now. Please try again in a moment.", REASON_BUSY)
	if status == 429 or matches(_USAGE_LIMIT_PATTERNS):
		epoch, resetText = resetTime(text)
		return error(
			KIND_USAGE_LIMIT,
			"You have hit the usage limit of your Claude plan. Please try again later.",
			resetText=resetText,
			resetEpoch=epoch,
		)
	if matches(_NETWORK_PATTERNS):
		return error(KIND_NETWORK, "Couldn't reach Claude. Check your internet connection and try again.")
	return error(KIND_OTHER, "Claude Code could not answer (exit code %s)." % returnCode)


def loginState(returnCode, stdout, stderr=""):
	"""(state, text) from `claude auth status --json`.
	authMethod values in Claude Code 2.1.314: "claude.ai" (subscription sign-in), "oauth_token"
	(a subscription token from claude setup-token), "api_key" and "api_key_helper" (Anthropic API,
	billed per use), "third_party" (Bedrock, Vertex, Foundry) and "none". The exit code is 0 when signed in.
	"""
	try:
		data = json.loads((stdout or "").strip())
	except ValueError:
		data = None
	if isinstance(data, dict):
		method = data.get("authMethod") if isinstance(data.get("authMethod"), str) else ""
		loggedIn = data.get("loggedIn") is True
		if not loggedIn or method == "none":
			return STATE_SIGNED_OUT, "Not signed in"
		if method in ("claude.ai", "oauth_token"):
			plan = data.get("subscriptionType")
			text = "Claude %s subscription" % plan.capitalize() if isinstance(plan, str) and plan else "Claude subscription"
			return STATE_SUBSCRIPTION, text
		return STATE_API_KEY, "Signed in with %s" % (method or "an API key")
	# Plain text (claude auth status --text), or an older version.
	cleaned = "\n".join(
		line.strip() for line in base.stripAnsi("%s\n%s" % (stdout, stderr)).splitlines() if line.strip()
	)
	lower = cleaned.lower()
	if returnCode != 0 or "not logged in" in lower:
		return STATE_SIGNED_OUT, cleaned
	if "api key" in lower or "console" in lower:
		return STATE_API_KEY, cleaned
	return STATE_SUBSCRIPTION, cleaned


def presetModels():
	return [modelCatalog.ModelInfo(slug, name, REASONING_EFFORTS, base.DEFAULT_REASONING_EFFORT) for slug, name in MODEL_ALIASES]


class ClaudeProvider(base.CliProvider):
	providerId = PROVIDER_ID
	displayName = "Claude (Claude Code)"
	shortName = "Claude"
	programName = "Claude Code"
	executableName = "claude"
	npmPackage = "@anthropic-ai/claude-code"
	installHelp = INSTALL_HELP
	strippedEnvironment = STRIPPED_ENVIRONMENT
	# `claude auth login --help`: "--claudeai  Use Claude subscription (default)".
	loginArgs = ("auth", "login", "--claudeai")
	logoutArgs = ("auth", "logout")
	# The link Claude Code prints for "If the browser didn't open" leads to a page with a code that is
	# typed back into the waiting `claude auth login` ("Paste code here if prompted >").
	loginAcceptsCode = True
	defaultEfforts = REASONING_EFFORTS
	imagesAttachedDirectly = False

	@classmethod
	def candidatePaths(cls, env=None, isWindows=base.IS_WINDOWS):
		return candidatePaths(env, isWindows)

	@classmethod
	def projectInstructions(cls, allowWrites=True):
		return PROJECT_INSTRUCTIONS if allowWrites else PROJECT_READ_ONLY_INSTRUCTIONS

	def find(self, userPath=""):
		if self._finder is not None:
			return self._finder(userPath)
		return findClaude(userPath, env=self._env, isWindows=self._isWindows)

	def models(self):
		"""Claude Code keeps no model list file, so offer its documented aliases."""
		return presetModels()

	def classifyFailure(self, returnCode, stdout, stderr, prompt=""):
		return classifyFailure(returnCode, stdout, stderr, prompt=prompt)

	def ask(self, prompt, cancelEvent=None, projectFolder=None, allowWrites=None, model=None, effort=None, attachments=None):
		"""Send prompt on stdin to `claude -p` and return the answer text. Details go to lastMeta.
		Attached images and files are opened by Claude with its Read tool from the request folder.
		"""
		projectFolder, allowWrites, model, effort = self._requestSettings(projectFolder, allowWrites, model, effort)
		self._checkProjectFolder(projectFolder)
		readableFolder = attachments.directory if attachments is not None and attachments.hasFiles else ""
		args = buildAskArgs(model, effort, projectFolder, allowWrites, readableFolder)
		returnCode, stdout, stderr = self._run(
			args,
			inputText=prompt,
			cancelEvent=cancelEvent,
			cwd=projectFolder or readableFolder or None,
		)
		result = parseResult(stdout)
		if result is None or result.failed or returnCode != 0:
			raise classifyFailure(returnCode, stdout, stderr, result, prompt)
		answer = result.text.strip()
		if not answer:
			raise ProviderError(
				KIND_OTHER,
				"Claude returned an empty answer.",
				base.tail(stderr),
				REASON_EMPTY_ANSWER,
				provider=PROVIDER_ID,
			)
		self.lastMeta = result.meta()
		return answer

	def loginStatus(self):
		"""Return (state, text) from `claude auth status --json`."""
		returnCode, stdout, stderr = self._run(["auth", "status", "--json"], timeout=30)
		return loginState(returnCode, stdout, stderr)
