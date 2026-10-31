# Wine smoke test for the Ask GPT add-on (QA only)

Runs the real add-on inside a real portable NVDA under Wine on Linux, with a fake Codex, so no
ChatGPT usage is spent. Nothing here is shipped; `build.py` only packages `addon/`.

* `fakecodex/codex.cmd`: fake Codex CLI (batch file, so the add-on's cmd.exe fallback runs). Handles
  `login status`, `logout` and `exec ... [-C DIR] -o FILE -`. Prompt words `SLOW`, `MEDIUM`, `FAIL401` select a
  24 s wait, a 3 s wait, or a 401 error; `MAKEFILE` writes `smoke-report.txt` into the `-C` project folder.
  Logs every call (arguments, `-o` file, `-C` folder and working directory) to `calls.log`.
* `fakecodex/models_cache.json`: copied to `%USERPROFILE%\.codex\models_cache.json` so the model list has two
  made-up models (plus a hidden one that must not be listed).
* `askGPTSmoke/`: throwaway NVDA add-on whose global plugin drives the Ask dialog, the conversation manager and the
  settings panel (gestures, SendInput keys, button events, the real format/file-name/rename dialogs), spies on
  `ui.message` (with the speech mode at that moment), `speech.speak`, `tones.beep` and `KeyboardInputGesture.send`,
  logs `ASKGPT-SMOKE PASS|FAIL|INFO` lines to nvda.log and exits NVDA. Windows+H is recorded but not sent (Wine has no
  voice typing and would type a literal "h"); dictated text is simulated by writing into the Question box.
  The native folder and file pickers cannot be driven under Wine, so `wx.DirDialog`/`wx.FileDialog` are briefly
  replaced by stand-ins that return a fixed path.
* `run.sh`: rebuilds the add-on, writes a fresh `userConfig` (silence synth, no welcome dialog,
  `codexPath = C:\fakecodex\codex.cmd`), resets the conversations folder, `C:\smokeproject` and `Documents\Ask GPT`,
  starts NVDA with debug logging on an Xvfb display, takes a screenshot at each test phase, checks that no fake child
  process outlives NVDA and that conversations were saved as JSON (`check-conversations.py`), and prints the results.

One-time setup (NVDA 2026.x fails under Wine 10.0: `GetConsoleSelectionInfo` is missing from kernel32):

    export WINEPREFIX=$HOME/nvda-wine/prefix WINEARCH=win64
    wineboot -i
    wine nvda_2025.3.3.exe --create-portable-silent --portable-path='C:\nvda'   # files appear; the launcher may hang on exit

Then run `tools/wine-smoke/run.sh` (pass `WINEPREFIX=` and `OUT=` if they are elsewhere). Output goes to
`$HOME/nvda-wine/out` by default. Wine cannot show real speech, UIA/IA2 semantics, Win+H voice typing, the Codex
Windows sandbox or a real Codex sign-in.
