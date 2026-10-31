# Ask AI, an NVDA add-on

Ask ChatGPT or Claude a question from anywhere in Windows by typing or by voice, and hear the answer read by NVDA.
The add-on drives the official command line programs of each service, signed in with your own subscription:

* **ChatGPT** through the [OpenAI Codex CLI](https://github.com/openai/codex), signed in with a ChatGPT plan (Plus, Pro, Team or Business).
* **Claude** through [Claude Code](https://docs.claude.com/en/docs/claude-code/overview), signed in with a Claude plan (Pro, Max, Team or Enterprise).

There is no API key and no pay-per-use billing. Ask AI was called **Ask GPT** up to version 1.1.

Full user documentation: [addon/doc/en/readme.html](addon/doc/en/readme.html). NVDA opens it from the Add-on Store's help button.
Licensed under the GNU General Public License version 2 ([LICENSE](LICENSE); shipped in the add-on as `copying.txt`).

## For users, in short

1. Install at least one of the programs:
   * ChatGPT: install Node.js, then run `npm install -g @openai/codex` in a command prompt.
   * Claude: in PowerShell run `irm https://claude.ai/install.ps1 | iex`
     (or `npm install -g @anthropic-ai/claude-code`, or `winget install Anthropic.ClaudeCode`).
2. Install `askAI-1.2.0.nvda-addon` and restart NVDA. Settings and conversations of Ask GPT 1.x are copied over,
   and Ask AI offers once to remove the old Ask GPT add-on (both would use NVDA+Alt+G).
3. NVDA menu, Preferences, Settings, **Ask AI**. Choose the service in **Account settings for**, then **Sign in to ChatGPT** or
   **Sign in to Claude** and finish the sign-in in the browser. **Default AI service** decides what new conversations use.
4. Press **NVDA+Alt+G**, type a question and press Enter. Alt+S chooses the AI service for the conversation
   (NVDA says "Now using Claude"). Alt+D dictates with Windows voice typing while NVDA stays quiet.
5. **NVDA+Alt+Shift+G** repeats the last answer. Press it twice to open the answer in a reviewable window.

New in 1.2.0:

* **Claude** as a second service, chosen per conversation; the model and effort lists follow the service.
  Answers are labelled "ChatGPT:" or "Claude:" in the Conversation box and in exports; the conversation manager has a Service column.
* Renamed to **Ask AI** (package `askAI`, data in `%LOCALAPPDATA%\askAI`), with migration from Ask GPT 1.x.
* **Update check**: once a day (and with "Check for updates now" in the settings or NVDA menu, Tools, "Check for Ask AI updates")
  the add-on looks for a newer GitHub release, verifies its SHA-256 checksum and hands it to NVDA's own installer.
  Skipped for copies installed from the NVDA Add-on Store, which updates them itself.
* API keys in the environment (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `OPENAI_API_KEY`, `CODEX_API_KEY`) are never passed
  to the programs, so usage always goes to your subscription.

Supports NVDA 2024.1 to 2026.x, including 64-bit NVDA 2026.1 on Python 3.13. The only ctypes code is the Windows
Job Object support in `providers/base.py` (kernel32 `CreateJobObjectW`, `SetInformationJobObject`, `AssignProcessToJobObject`,
`TerminateJobObject`, `CloseHandle`). It uses a private `WinDLL` with full `argtypes`/`restype` (handles are `HANDLE`, i.e. `c_void_p`)
and pointer-sized structure fields, and is loaded only on Windows.

## Layout

```
addon/manifest.ini
addon/copying.txt                                  GPL v2 text, at the root of the .nvda-addon
addon/globalPlugins/askAI/__init__.py              GlobalPlugin: config spec, migration, Tools menu items, gestures, terminate()
addon/globalPlugins/askAI/providers/__init__.py    Pure: registry of services (id, display name), ProviderError, states
addon/globalPlugins/askAI/providers/base.py        Pure: shared process runner (stdin prompt, Job Object, kill, cmd.exe quoting,
                                                   npm shim resolution, env stripping, timeouts, cancel, workdir), prompt building
addon/globalPlugins/askAI/providers/codex.py       Pure: ChatGPT through `codex exec`, error classification, `codex login`
addon/globalPlugins/askAI/providers/claude.py      Pure: Claude through `claude -p`, JSON result parsing, `claude auth`
addon/globalPlugins/askAI/providers/modelCatalog.py Pure: model lists (Codex's models_cache.json) and chooser helpers
addon/globalPlugins/askAI/conversations.py         Pure: Conversation and ConversationStore (JSON files, atomic writes, schema 2)
addon/globalPlugins/askAI/migration.py             Pure: copying Ask GPT 1.x settings and conversations, finding the old add-on
addon/globalPlugins/askAI/updater.py               Pure: GitHub release check, version rules, checksum-verified download
addon/globalPlugins/askAI/dictation.py             Pure: mute/restore state machine for dictation (speech module injected)
addon/globalPlugins/askAI/exporters.py             Pure: .docx, .pdf, .txt, .html and .md writers, safe file names
addon/globalPlugins/askAI/folderSnapshot.py        Pure: find files created or changed in the project folder
addon/globalPlugins/askAI/textUtils.py             Pure: Markdown stripping before speech, transcript formatting
addon/globalPlugins/askAI/dialogs.py               wx Ask dialog, SettingsPanel, request lifecycle (NVDA only)
addon/globalPlugins/askAI/conversationManager.py   wx conversation manager dialog (NVDA only)
addon/globalPlugins/askAI/updateCheck.py           NVDA side of the update check (timer, prompts, NVDA's installer)
addon/doc/en/readme.html                           User documentation
build.py                                           Builds askAI-<version>.nvda-addon with zipfile
tests/                                             unittest suite, runs on Linux
tester/TESTING.txt                                 Guide for human testers
tools/wine-smoke/                                  QA only: drives the real add-on in NVDA under Wine with a fake Codex and Claude Code
.github/workflows/                                 CI (tests + build) and tag-driven releases; see RELEASING.md
```

## How a question is run

The prompt, made of the instructions, the earlier conversation (the last 10 answered exchanges and at most about 12,000 characters)
and the question, is always written to **stdin** as UTF-8. User text never appears on the command line, and neither do the instructions
(no `--append-system-prompt`). With a project folder, the service's project folder rules are added to the instructions.

### ChatGPT (Codex)

```
codex exec --skip-git-repo-check --sandbox read-only --color never -c approval_policy=never \
           -o <workdir>/answer-XXXX.txt [-m <model>] [-c model_reasoning_effort=<effort>] -
```

and, when the conversation has a project folder:

```
codex exec --skip-git-repo-check --sandbox workspace-write --color never -c approval_policy=never \
           -C <project> -c sandbox_workspace_write.network_access=false \
           -c sandbox_workspace_write.exclude_tmpdir_env_var=true -c sandbox_workspace_write.exclude_slash_tmp=true \
           -o <workdir>/answer-XXXX.txt [-m <model>] [-c model_reasoning_effort=<effort>] -
```

(`--sandbox read-only` with `-C <project>` when "Allow the AI to create and edit files in the project folder" is off.)
`-c` values are unquoted: Codex parses them as TOML and uses non-TOML text as a plain string (checked with codex-cli 0.160.0).
The answer is read from the `-o` file, with trimmed stdout as the fallback. `OPENAI_API_KEY` and `CODEX_API_KEY` are removed from
Codex's environment (codex-cli 0.160.0 accepts them as "a supported auth env var" instead of the ChatGPT sign-in).

### Claude (Claude Code)

```
claude -p --output-format json --no-session-persistence --strict-mcp-config --restricted \
       --tools <tools> --disallowedTools "mcp__*" --permission-mode <mode> --permission-prompts none \
       [--model <model>] [--effort <effort>]
```

with the prompt on stdin (`-p` without a prompt argument reads stdin) and the working directory set to the project folder,
or the private work folder. Verified against Claude Code 2.1.314 (`claude --help`) and the Claude Code docs:

| Conversation | `--tools` | `--permission-mode` |
| - | - | - |
| No project folder | `""` (no tools at all) | `dontAsk` |
| Project folder, writing allowed | `Read,Write,Edit,Glob,Grep` | `acceptEdits` |
| Project folder, read-only | `Read,Glob,Grep` | `dontAsk` |

* `--tools` limits the built-in tools, so Bash, PowerShell, WebFetch, WebSearch and Task are never available. `--tools` does not cover MCP
  tools: `--strict-mcp-config` without `--mcp-config` loads no MCP servers, and `--disallowedTools "mcp__*"` denies any that remain.
* `--restricted` (Claude Code 2.1.248+) confines the file tools to the working directory and ignores user, project and local settings
  files, so a project folder's `.claude/settings.json` hooks or permission rules never run. `--permission-prompts none` (2.1.259+)
  denies anything that would ask a person. `bypassPermissions` and `--dangerously-skip-permissions` are never used.
  An older Claude Code rejects these options; the add-on then asks the user to run `claude update`.
* The JSON result gives `result`, `is_error`, `subtype`, `session_id` and `usage`. Errors are classified from Claude Code's result text,
  `api_error_status` and the end of stderr: not signed in ("Not logged in · Please run /login", "Invalid API key", status 401/403),
  usage limit ("You've hit your session limit · resets 3pm (Europe/London)" or the older "Claude AI usage limit reached|<epoch>", whose
  reset time is spoken in local time), busy ("Server is temporarily limiting requests (not your usage limit)", 529), network, other.
* `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are removed from Claude Code's environment, because with them Claude Code would use
  and bill the Anthropic API instead of the subscription. `claude auth status --json` (`authMethod`: `claude.ai` or `oauth_token` =
  subscription, `api_key` / `api_key_helper` / `third_party` = billed per use) drives the "Signed in with an API key" warning.
* Sign-in runs `claude auth login --claudeai`. It opens the browser; the "If the browser didn't open, visit:" link from its output is
  offered by "Copy sign-in link". That link ends on a page showing a code, which "Enter sign-in code" types into the waiting
  `claude auth login` (its stdin is kept open for this). `claude auth logout` signs out.
* Claude Code keeps no model list file, so the model choice offers its documented aliases (sonnet, opus, haiku, fable) and any typed name;
  the effort choice offers `--effort` levels low, medium, high, xhigh and max.

**Honestly about Windows:** Claude Code has no operating-system sandbox on native Windows. The add-on's protection is that Claude may only use
the file tools listed above, confined by Claude Code to the project folder, and has no command or web tools. Claude can therefore only make
text-based files (for example Markdown, HTML, CSV); for Word or PDF the user saves the answer with "Save answer as".

### Shared process handling

* The working directory is the project folder, or an empty private folder, `%LOCALAPPDATA%\askAI\workdir` (or the temp folder).
  Codex's `-o` answer file always stays in the private folder. Stale `answer-*.txt` files are deleted at startup.
* Project paths are passed to Codex's `-C` normalised and never ending in a backslash (`C:\` becomes `C:\.`), so the cmd.exe quoting rules hold.
* On Windows a process starts with `CREATE_NO_WINDOW` and is put in a kill-on-close Job Object, so no program outlives NVDA.
  On other systems it gets its own process group (`start_new_session`), which is killed as a whole.
* npm `.cmd` shims are resolved without cmd.exe: `codex.cmd` to `node <codex.js>` (Node.js is looked for next to the shim, on PATH and in
  `%ProgramFiles%\nodejs`), `claude.cmd` to its native `claude.exe`. Only unrecognised `.cmd` files run through
  `%SystemRoot%\System32\cmd.exe /d /v:off /s /c`, with every argument quoted and `"`, `%` and line breaks refused.
* Claude Code is looked for in the configured path, on PATH, in `%USERPROFILE%\.local\bin\claude.exe` (native installer),
  `%APPDATA%\npm\claude.cmd` (npm) and `%LOCALAPPDATA%\Microsoft\WinGet\Links\claude.exe` (winget).
* Cancel, timeout and NVDA exit kill the process tree: the Job Object, falling back to `%SystemRoot%\System32\taskkill.exe /T /F`.
* Failures become a `ProviderError` with a `kind` (notInstalled, notLoggedIn, usageLimit, network, timeout, cancelled, config, other) and the
  service's id, so every message names the right service ("Sign in to Claude in NVDA Settings > Ask AI").
  The spoken message is always plain language. Technical detail goes to the NVDA log and, after "Technical details:", to the Conversation box.

All program calls run in daemon threads. UI updates go through `wx.CallAfter`, and every callback checks that its dialog or panel still exists.
A question belongs to the module and to the conversation it was asked in (by id), not to the dialog.

### Conversations

One JSON file per conversation (schema 2). It stores `provider` ("codex" or "claude") for the conversation and on every answer.
Schema 1 files from Ask GPT 1.x load as ChatGPT conversations.

### Windows sandbox for Codex, honestly

Without a project folder Codex runs `read-only`. With a project folder and writing allowed it runs `workspace-write`: per the Codex docs
network access is off by default (the add-on also sets `sandbox_workspace_write.network_access=false` explicitly), `.git`, `.codex` and
`.agents` stay read-only, and on native Windows Codex enforces this with its Windows sandbox (MXC where supported, otherwise the legacy
`elevated` or `unelevated` implementations). The docs do not say what `codex exec` does on Windows when no Windows sandbox has been set up,
and this was not tested on real Windows. The add-on never passes `--dangerously-bypass-approvals-and-sandbox` or `danger-full-access`.

## Development

```
python3 -m unittest discover -s tests -v     # all tests; no NVDA, Codex or Claude Code needed (never calls the real programs)
python3 build.py                             # writes askAI-1.2.0.nvda-addon
python3 -m py_compile addon/globalPlugins/askAI/*.py addon/globalPlugins/askAI/providers/*.py build.py tests/*.py
WINEPREFIX=... OUT=... bash tools/wine-smoke/run.sh   # optional: real NVDA under Wine, fake Codex and Claude Code
```

Releases: see [RELEASING.md](RELEASING.md).

Code follows NVDA conventions: tabs, camelCase, `addonHandler.initTranslation()`, and user-visible strings in `_()` with
`# Translators:` comments. The pure modules must not import NVDA modules, so they stay testable.
`tests/test_nvdaIntegration.py` loads the NVDA-facing modules against minimal stand-ins for NVDA and a fake provider. It checks
registration, cleanup, gestures, the request lifecycle across conversations and services, migration, and mnemonics, but it is no
substitute for testing in NVDA on Windows.
