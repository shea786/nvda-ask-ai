#!/usr/bin/env bash
# Runs the Ask AI add-on in a real portable NVDA under Wine, driven by the askAISmoke plugin, with a
# fake codex.cmd and a fake claude.cmd. Prints the ASKAI-SMOKE lines. Screenshots and the full log go to $OUT.
# Prerequisite (once): a portable NVDA at C:\nvda in $WINEPREFIX, e.g.
#   wine nvda_2025.3.3.exe --create-portable-silent --portable-path='C:\nvda'
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export WINEPREFIX="${WINEPREFIX:-$HOME/nvda-wine/prefix}"
export WINEDEBUG="${WINEDEBUG:--all}"
export DISPLAY="${SMOKE_DISPLAY:-:77}"
OUT="${OUT:-$HOME/nvda-wine/out}"
WINESERVER="${WINESERVER:-/usr/lib/x86_64-linux-gnu/wine/wineserver}"
C="$WINEPREFIX/drive_c"
CONF="$C/nvda/userConfig"
NVDA_EXE="${NVDA_EXE:-nvda.exe}"
# ASKAI_SMOKE_ONLY=new runs only the Ask AI 1.2 steps (see askAISmoke.py); Wine passes it on to NVDA.
export ASKAI_SMOKE_ONLY="${ASKAI_SMOKE_ONLY:-}"
MIN_CONVERSATIONS=3
[ "$ASKAI_SMOKE_ONLY" = new ] && MIN_CONVERSATIONS=1

[ -f "$C/nvda/$NVDA_EXE" ] || { echo "No portable NVDA at C:\\nvda"; exit 1; }
if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
	Xvfb "$DISPLAY" -screen 0 1280x1024x24 -nolisten tcp >/dev/null 2>&1 &
	sleep 1
fi

# Fresh user config with the add-ons extracted, as NVDA would after installing them: Ask AI, the smoke
# driver, and a stand-in for the old Ask GPT 1.x add-on (manifest only), which Ask AI should offer to remove.
ADDON="$(python3 "$REPO/build.py" | sed 's/^Built //')"
rm -rf "$CONF" "$OUT"
mkdir -p "$CONF/addons/askAI" "$CONF/addons/askGPT" "$OUT"
python3 -I -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \
	"$ADDON" "$CONF/addons/askAI"
cp -r "$HERE/askAISmoke" "$CONF/addons/askAISmoke"
cat > "$CONF/addons/askGPT/manifest.ini" <<'EOF'
name = askGPT
summary = "Ask GPT"
description = """Stand-in for Ask GPT 1.1 in the smoke test (no code)."""
author = "Ask AI QA"
version = 1.1.0
minimumNVDAVersion = 2024.1
lastTestedNVDAVersion = 2026.1
EOF
cat > "$CONF/nvda.ini" <<'EOF'
schemaVersion = 18
[general]
	showWelcomeDialogAtStartup = False
	askToExit = False
	playStartAndExitSounds = False
[update]
	autoCheck = False
	startupNotification = False
	askedAllowUsageStats = True
	allowUsageStats = False
[speech]
	synth = silence
[askGPT]
	codexPath = C:\fakecodex\codex.cmd
	beepWhileWaiting = True
	dictationSilenceTimeout = 3
EOF

# The fake Codex and Claude Code, with CRLF line endings as cmd.exe expects.
rm -rf "$C/fakecodex" "$C/fakeclaude"
mkdir -p "$C/fakecodex" "$C/fakeclaude"
sed 's/\r\?$/\r/' "$HERE/fakecodex/codex.cmd" > "$C/fakecodex/codex.cmd"
sed 's/\r\?$/\r/' "$HERE/fakeclaude/claude.cmd" > "$C/fakeclaude/claude.cmd"
rm -f "$C/nvda-smoke.log"
# A stale answer file, as if NVDA had crashed mid-question; the add-on should delete it at startup.
WORKDIR="$C/users/$USER/AppData/Local/askAI/workdir"
mkdir -p "$WORKDIR"
echo stale > "$WORKDIR/answer-stale.txt"
# Fresh saved conversations, project folder and Documents\Ask AI for the conversation and export steps.
CONVERSATIONS="$C/users/$USER/AppData/Local/askAI/conversations"
OLD_CONVERSATIONS="$C/users/$USER/AppData/Local/askGPT/conversations"
rm -rf "$CONVERSATIONS" "$OLD_CONVERSATIONS" "$C/smokeproject" "$C/users/$USER/Documents/Ask GPT" "$C/users/$USER/Documents/Ask AI"
# One archived Ask GPT 1.1 conversation (schema 1, no provider) that Ask AI should copy on first start.
mkdir -p "$OLD_CONVERSATIONS"
cat > "$OLD_CONVERSATIONS/0123456789abcdef0123456789abcdef.json" <<'EOF'
{"schemaVersion": 1, "id": "0123456789abcdef0123456789abcdef", "title": "From Ask GPT", "titleIsCustom": false,
 "created": 1700000000.0, "updated": 1700000000.0, "archived": true, "projectFolder": "", "model": "",
 "reasoningEffort": "low", "messages": [{"role": "user", "text": "Old question", "time": 1700000000.0},
 {"role": "assistant", "text": "Old answer", "time": 1700000001.0}]}
EOF
mkdir -p "$C/smokeproject" "$C/users/$USER/Documents"
echo "already here" > "$C/smokeproject/existing.txt"
# A Codex model cache like the real one (%USERPROFILE%\.codex\models_cache.json), with made-up models.
mkdir -p "$C/users/$USER/.codex"
cp "$HERE/fakecodex/models_cache.json" "$C/users/$USER/.codex/models_cache.json"

cd /tmp
timeout 780 wine "C:\\nvda\\$NVDA_EXE" --debug-logging "--log-file=C:\\nvda-smoke.log" >"$OUT/wine-stdout.log" 2>&1 &
WINE_PID=$!

# Screenshot whenever the plugin announces a new phase.
last=""
n=0
while kill -0 "$WINE_PID" 2>/dev/null; do
	current="$(cat "$C/fakecodex/phase.txt" 2>/dev/null || true)"
	if [ -n "$current" ] && [ "$current" != "$last" ]; then
		n=$((n + 1))
		import -display "$DISPLAY" -window root "$OUT/$(printf %02d "$n")-$current.png" || true
		last="$current"
		if [ "$current" = exit ]; then
			for _ in $(seq 60); do kill -0 "$WINE_PID" 2>/dev/null || break; sleep 0.5; done
			kill -0 "$WINE_PID" 2>/dev/null && echo "NVDA still running 30 s after exit was triggered; killing wineserver" \
				|| echo "NVDA process exited by itself"
			break
		fi
	fi
	sleep 0.3
done
# Exit was triggered with a slow (about 24 s) fake request running; terminate() should have killed it.
sleep 2
if pgrep -f 'ping.exe' >/dev/null; then echo "SHELL FAIL ping.exe from the fake codex survived NVDA exit"; \
	else echo "SHELL PASS no fake codex child (ping.exe) left after NVDA exit"; fi
if grep -q SLOW-FINISHED "$C/fakecodex/calls.log"; then echo "SHELL FAIL a slow fake request ran to completion"; \
	else echo "SHELL PASS no slow fake request ran to completion"; fi
# Conversations are saved as JSON files, one per conversation, readable after NVDA exits.
python3 -I "$HERE/check-conversations.py" "$CONVERSATIONS" "$OLD_CONVERSATIONS" "$MIN_CONVERSATIONS" || true
"$WINESERVER" -k 2>/dev/null || true
# After a wineserver crash the wine loader can wait forever on a defunct nvda.exe.
kill "$WINE_PID" 2>/dev/null || true
wait "$WINE_PID" 2>/dev/null || true
grep -q "add_queue_hook_count: Assertion" "$OUT/wine-stdout.log" 2>/dev/null \
	&& echo "NOTE wineserver crashed (Wine bug: queue.c add_queue_hook_count assertion); steps after that point did not run"

cp "$C/nvda-smoke.log" "$OUT/nvda.log" 2>/dev/null || true
cp "$C/fakecodex/calls.log" "$OUT/fake-codex-calls.log" 2>/dev/null || true
cp "$C/fakeclaude/calls.log" "$OUT/fake-claude-calls.log" 2>/dev/null || true
grep -a "ASKAI-SMOKE" "$OUT/nvda.log" | grep -av "ASKAI-SMOKE INFO" || true
echo "Full log: $OUT/nvda.log   Screenshots: $OUT/*.png"
