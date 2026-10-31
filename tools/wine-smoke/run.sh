#!/usr/bin/env bash
# Runs the Ask GPT add-on in a real portable NVDA under Wine, driven by the askGPTSmoke plugin, with a
# fake codex.cmd. Prints the ASKGPT-SMOKE lines. Screenshots and the full log go to $OUT.
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

[ -f "$C/nvda/$NVDA_EXE" ] || { echo "No portable NVDA at C:\\nvda"; exit 1; }
if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
	Xvfb "$DISPLAY" -screen 0 1280x1024x24 -nolisten tcp >/dev/null 2>&1 &
	sleep 1
fi

# Fresh user config with both add-ons extracted, as NVDA would after installing them.
python3 "$REPO/build.py" >/dev/null
rm -rf "$CONF" "$OUT"
mkdir -p "$CONF/addons/askGPT" "$OUT"
python3 -I -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \
	"$REPO/askGPT-1.0.0.nvda-addon" "$CONF/addons/askGPT"
cp -r "$HERE/askGPTSmoke" "$CONF/addons/askGPTSmoke"
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
EOF

# The fake Codex, with CRLF line endings as cmd.exe expects.
rm -rf "$C/fakecodex"
mkdir -p "$C/fakecodex"
sed 's/\r\?$/\r/' "$HERE/fakecodex/codex.cmd" > "$C/fakecodex/codex.cmd"
rm -f "$C/nvda-smoke.log"
# A stale answer file, as if NVDA had crashed mid-question; the add-on should delete it at startup.
WORKDIR="$C/users/$USER/AppData/Local/askGPT/workdir"
mkdir -p "$WORKDIR"
echo stale > "$WORKDIR/answer-stale.txt"

cd /tmp
timeout 420 wine "C:\\nvda\\$NVDA_EXE" --debug-logging "--log-file=C:\\nvda-smoke.log" >"$OUT/wine-stdout.log" 2>&1 &
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
"$WINESERVER" -k 2>/dev/null || true
wait "$WINE_PID" 2>/dev/null || true

cp "$C/nvda-smoke.log" "$OUT/nvda.log" 2>/dev/null || true
cp "$C/fakecodex/calls.log" "$OUT/fake-codex-calls.log" 2>/dev/null || true
grep -a "ASKGPT-SMOKE" "$OUT/nvda.log" | grep -av "ASKGPT-SMOKE INFO" || true
echo "Full log: $OUT/nvda.log   Screenshots: $OUT/*.png"
