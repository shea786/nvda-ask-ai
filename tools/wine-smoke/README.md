# Wine smoke test for the Ask AI add-on (QA only)

Runs the real add-on inside a real portable NVDA under Wine on Linux, with a fake Codex and a fake Claude Code,
so no ChatGPT or Claude usage is spent. Nothing here is shipped; `build.py` only packages `addon/`.

* `fakecodex/codex.cmd`: fake Codex CLI (batch file, so the add-on's cmd.exe fallback runs). Handles
  `login status`, `logout` and `exec ... [-C DIR] -o FILE -`. Prompt words `SLOW`, `MEDIUM`, `FAIL401` select a
  24 s wait, a 3 s wait, or a 401 error; `MAKEFILE` writes `smoke-report.txt` into the `-C` project folder.
  Logs every call (arguments, `-o` file, `-C` folder and working directory) to `calls.log`.
* `fakecodex/models_cache.json`: copied to `%USERPROFILE%\.codex\models_cache.json` so the model list has two
  made-up models (plus a hidden one that must not be listed).
* `fakeclaude/claude.cmd`: fake Claude Code (also a batch file). Handles `auth status [--json]` (prints signed-in
  JSON shaped like Claude Code 2.1.314), `auth logout` and `-p ...` (stdin saved to `last-stdin.txt`, prints a JSON
  result). Prompt word `CLAUDE401` prints Claude Code's not-signed-in result; `CLAUDEFILE` writes `claude-report.md`
  into the working directory (the project folder). Logs arguments and working directory to its own `calls.log`.
* `askAISmoke/`: throwaway NVDA add-on whose global plugin drives the Ask dialog, the conversation manager and the
  settings panel (gestures, SendInput keys, button events, the real format/file-name/rename dialogs), spies on
  `ui.message` (with the speech mode at that moment), `speech.speak`, `tones.beep` and `KeyboardInputGesture.send`,
  logs `ASKAI-SMOKE PASS|FAIL|INFO` lines to nvda.log and exits NVDA. Windows+H is recorded but not sent (Wine has no
  voice typing and would type a literal "h"); dictated text is simulated by writing into the Question box.
  The native folder and file pickers cannot be driven under Wine, so `wx.DirDialog`/`wx.FileDialog` are briefly
  replaced by stand-ins that return a fixed path.
  Ask AI 1.2 checks: migration from Ask GPT (settings from an `[askGPT]` section, an archived schema 1 conversation
  copied from `%LOCALAPPDATA%\askGPT\conversations`), the one-time question about removing the old add-on (a manifest-only
  stand-in `askGPT` add-on is installed; the test answers No), switching a conversation to Claude ("Now using Claude",
  Claude's model and effort lists), the Claude answer path (argv, stdin, "Claude:" label, provider stored), Claude in a
  project folder (file tools only, `acceptEdits`, cwd), a Claude sign-in error, the Service column, and both services in
  the settings ("Default AI service", "Account settings for", Claude's sign-in labels, path and sign-in code button).
  Round 6 checks (`_round6a`): Ctrl+F through the real find prompt, F3 / Shift+F3 with wrap-around, "No matches for", Up/Down
  question recall, Ctrl+S drafts restored on reopening, NVDA+Alt+Shift+S while answering and idle, and "Ask AI answer, <title>:" when the
  answer arrives with the dialog closed. (`_round6b`): NVDA+Alt+A on a selected multi-line edit (a window in NVDA's own process,
  because Wine without a window manager never brings Notepad to the foreground) with the Attached list and a screenshot sent as
  `--image=`, refusal on a password field, Attach files (text inlined, picture as `--image=` for Codex, `--tools Read` in the
  request folder for Claude), a long paste sent as question.txt, and the conversation manager's Search field.
  Round 7 (the 1.2.0 security and accessibility fixes) updated these steps: the old add-on question comes on the first Ask
  dialog (a Yes/No dialog where Escape asks again), "Ask AI answer, <title>:", the status wording and locale time, the
  "Attached to next question" list (source and size of the screen text, screenshot box naming the window, the picture taken
  only when sending), ChatGPT refusing a file that is not text or a picture, Codex hardening flags, Claude's `--safe-mode`,
  switching service through Conversation options, Search's "N match; first: <title>" and Enter continuing the first match,
  and the Settings changes (grouped account section, "Claude: Signed in with Claude", no update button).
  `ASKAI_SMOKE_ONLY=oldaddon` runs the whole old add-on question (Escape, asked again, No) and Escape closing the dialog
  while a request keeps running; the round6 runs answer No straight away to keep the number of windows down.
  The `new` mode and the full run still drive the round 5 service controls in the Ask dialog and were not updated.
* `run.sh`: rebuilds the add-on, writes a fresh `userConfig` (silence synth, no welcome dialog, an `[askGPT]` section with
  `codexPath = C:\fakecodex\codex.cmd` for the migration), resets the conversation folders, `C:\smokeproject` and
  `Documents\Ask AI`, starts NVDA with debug logging on an Xvfb display, takes a screenshot at each test phase, checks that no
  fake child process outlives NVDA and that conversations were saved as JSON with their service (`check-conversations.py`),
  and prints the results.

One-time setup (NVDA 2026.x fails under Wine 10.0: `GetConsoleSelectionInfo` is missing from kernel32):

    export WINEPREFIX=$HOME/nvda-wine/prefix WINEARCH=win64
    wineboot -i
    wine nvda_2025.3.3.exe --create-portable-silent --portable-path='C:\nvda'   # files appear; the launcher may hang on exit

Then run `tools/wine-smoke/run.sh` (pass `WINEPREFIX=` and `OUT=` if they are elsewhere; `ASKAI_SMOKE_ONLY=new` runs only
the Ask AI 1.2 steps (`round6`, or `round6a`, `round6b`, `round6c` for a third of them each, or `oldaddon`), which helps when Wine 10's wineserver crashes with `add_queue_hook_count: Assertion ... failed`
after many short-lived processes; that crash also happens with the unchanged 1.1 harness). Output goes to
`$HOME/nvda-wine/out` by default. Wine cannot show real speech, UIA/IA2 semantics, Win+H voice typing, the Codex
Windows sandbox, a real Codex or Claude Code sign-in, or a real GitHub update download.
