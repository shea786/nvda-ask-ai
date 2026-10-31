# Ask GPT, an NVDA add-on

Ask ChatGPT a question from anywhere in Windows by typing or by voice, and hear the answer read by NVDA.
The add-on drives the official [OpenAI Codex CLI](https://github.com/openai/codex), which signs in with a
ChatGPT account. Usage goes against the ChatGPT plan (Plus, Pro, Team or Business). There is no API key and no pay-per-use billing.

Full user documentation: [addon/doc/en/readme.html](addon/doc/en/readme.html). NVDA opens it from the Add-on Store's help button.

## For users, in short

1. Install Node.js, then run `npm install -g @openai/codex` in a command prompt
   (or download `codex` from github.com/openai/codex/releases).
2. Install `askGPT-1.0.0.nvda-addon` and restart NVDA.
3. NVDA menu, Preferences, Settings, **Ask GPT**, then **Sign in with ChatGPT**. Finish the sign-in in the browser.
4. Press **NVDA+Alt+G**, type a question and press Enter. To dictate with Windows voice typing (Win+H), press Alt+D, then Enter to send.
5. **NVDA+Alt+Shift+G** repeats the last answer. Press it twice to open the answer in a reviewable window.

Supports NVDA 2024.1 to 2026.x, including 64-bit NVDA 2026.1 on Python 3.13. The add-on contains no ctypes code.

## Layout

```
addon/manifest.ini
addon/globalPlugins/askGPT/__init__.py     GlobalPlugin: config spec, Tools menu item, gestures, settings panel registration, terminate()
addon/globalPlugins/askGPT/codexClient.py  Pure stdlib: find codex, build argv, run `codex exec` with the prompt on stdin, errors, login helpers
addon/globalPlugins/askGPT/textUtils.py    Pure: Markdown stripping before speech, transcript formatting
addon/globalPlugins/askGPT/dialogs.py      wx Ask dialog and SettingsPanel (NVDA only)
addon/doc/en/readme.html                   User documentation
build.py                                   Builds askGPT-<version>.nvda-addon with zipfile
tests/                                     unittest suite, runs on Linux
```

## How a question is run

`codexClient.CodexClient.ask()` runs:

```
codex exec --skip-git-repo-check --sandbox read-only --color never -c approval_policy="never" \
           -o <workdir>/answer-XXXX.txt [-m <model>] [-c model_reasoning_effort="<effort>"] -
```

* The prompt, made of the instructions, the earlier conversation (the last 10 exchanges and at most about 12,000 characters) and the question,
  is written to **stdin** as UTF-8. User text never appears on the command line.
* The working directory is an empty folder, `<NVDA config>/askGPT-workdir`.
* On Windows the process starts with `CREATE_NO_WINDOW`. An npm `codex.cmd` shim is resolved to `node <codex.js>` when possible.
  Otherwise it runs through `cmd.exe /d /s /c "..."`, which only ever receives fixed arguments and validated paths.
* The answer is read from the `-o` file, with trimmed stdout as the fallback.
* Cancel and timeout kill the process tree (`taskkill /T /F /PID` on Windows).
* Failures become a `CodexError` with a `kind`: notInstalled, notLoggedIn, usageLimit, timeout, cancelled, config or other.
  The classification matches text heuristically, because Codex has no stable error codes.

All Codex calls run in daemon threads. UI updates go through `wx.CallAfter`, and every callback checks that its dialog or panel still exists.

## Development

```
python3 -m unittest discover -s tests -v     # all tests; no NVDA or Codex needed
python3 build.py                             # writes askGPT-1.0.0.nvda-addon
python3 -m py_compile addon/globalPlugins/askGPT/*.py build.py tests/*.py
```

Code follows NVDA conventions: tabs, camelCase, `addonHandler.initTranslation()`, and user-visible strings in `_()` with
`# Translators:` comments. `codexClient.py` and `textUtils.py` must not import NVDA modules, so they stay testable.
`tests/test_nvdaIntegration.py` loads the NVDA-facing modules against minimal stand-ins for NVDA. It checks registration,
cleanup and gestures, but it is no substitute for testing in NVDA on Windows.
