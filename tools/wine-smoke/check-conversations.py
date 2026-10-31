"""Run by run.sh after NVDA exits: the add-on saved its conversations as JSON files (one per conversation),
recorded the service of each conversation and answer, and copied (not moved) the Ask GPT 1.1 conversation.
"""

import glob
import json
import os
import sys

folder = sys.argv[1]
oldFolder = sys.argv[2] if len(sys.argv) > 2 else ""
minimum = int(sys.argv[3]) if len(sys.argv) > 3 else 3
expectClaude = (sys.argv[4] if len(sys.argv) > 4 else "1") == "1"
MIGRATED_ID = "0123456789abcdef0123456789abcdef"
files = sorted(glob.glob(os.path.join(folder, "*.json")))
data = []
for path in files:
	with open(path, encoding="utf-8") as f:
		data.append(json.load(f))
byId = {d.get("id"): d for d in data}
current = [d for d in data if d.get("id") != MIGRATED_ID]
ok = len(current) >= minimum and all(
	d.get("schemaVersion") == 3 and d.get("id", "") + ".json" == os.path.basename(path) and d.get("provider") in ("codex", "claude")
	for d, path in zip(data, files)
	if d.get("id") != MIGRATED_ID
)
print(
	"SHELL %s conversations saved as JSON (schema 3, with provider) after exit | %d files: %s"
	% ("PASS" if ok else "FAIL", len(files), [(d.get("title"), d.get("provider"), len(d.get("messages", []))) for d in data]),
)
claudeAnswers = [
	m
	for d in current
	for m in d.get("messages", [])
	if m.get("role") == "assistant" and m.get("provider") == "claude"
]
claudeConversations = [d for d in current if d.get("provider") == "claude"]
if expectClaude:
	print(
		"SHELL %s a Claude answer was stored with provider claude | conversations=%d answers=%d"
		% ("PASS" if claudeAnswers else "FAIL", len(claudeConversations), len(claudeAnswers)),
	)
untagged = [
	(d.get("title"), m.get("text", "")[:30])
	for d in current
	for m in d.get("messages", [])
	if m.get("role") == "assistant" and m.get("provider") not in ("codex", "claude")
]
print("SHELL %s every saved answer records its service | %r" % ("FAIL" if untagged else "PASS", untagged))
migrated = byId.get(MIGRATED_ID)
oldStillThere = bool(oldFolder) and os.path.isfile(os.path.join(oldFolder, MIGRATED_ID + ".json"))
print(
	"SHELL %s the Ask GPT conversation was copied (old file kept) | migrated=%r oldKept=%s"
	% (
		"PASS" if migrated and migrated.get("title") == "From Ask GPT" and oldStillThere else "FAIL",
		(migrated or {}).get("title"),
		oldStillThere,
	),
)
leftovers = [name for name in os.listdir(folder) if name.startswith(".tmp")] if os.path.isdir(folder) else []
print("SHELL %s no temporary conversation files left | %r" % ("FAIL" if leftovers else "PASS", leftovers))
