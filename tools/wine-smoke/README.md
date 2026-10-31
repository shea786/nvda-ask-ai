# Wine smoke test for the Ask GPT add-on (QA only)

Runs the real add-on inside a real portable NVDA under Wine on Linux, with a fake Codex, so no
ChatGPT usage is spent. Nothing here is shipped; `build.py` only packages `addon/`.

* `fakecodex/codex.cmd`: fake Codex CLI (batch file, so the add-on's cmd.exe fallback runs). Handles
  `login status`, `logout` and `exec ... -o FILE -`. Prompt words `SLOW`, `MEDIUM`, `FAIL401` select a
  24 s wait, a 3 s wait, or a 401 error. Logs every call to `calls.log`.
* `askGPTSmoke/`: throwaway NVDA add-on whose global plugin drives the Ask dialog and the settings panel
  (gestures, SendInput keys, button events), spies on `ui.message`, `speech.speak` and `tones.beep`, logs
  `ASKGPT-SMOKE PASS|FAIL|INFO` lines to nvda.log and exits NVDA.
* `run.sh`: rebuilds the add-on, writes a fresh `userConfig` (silence synth, no welcome dialog,
  `codexPath = C:\fakecodex\codex.cmd`), starts NVDA with debug logging on an Xvfb display, takes a
  screenshot at each test phase, checks that no fake child process outlives NVDA, and prints the results.

One-time setup (NVDA 2026.x fails under Wine 10.0: `GetConsoleSelectionInfo` is missing from kernel32):

    export WINEPREFIX=$HOME/nvda-wine/prefix WINEARCH=win64
    wineboot -i
    wine nvda_2025.3.3.exe --create-portable-silent --portable-path='C:\nvda'   # files appear; the launcher may hang on exit

Then run `tools/wine-smoke/run.sh`. Output goes to `$HOME/nvda-wine/out` (override with `OUT=`).
Wine cannot show real speech, UIA/IA2 semantics, Win+H voice typing or a real Codex sign-in.
