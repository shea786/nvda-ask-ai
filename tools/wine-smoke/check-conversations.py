"""Run by run.sh after NVDA exits: the add-on saved its conversations as JSON files (one per conversation)."""

import glob
import json
import os
import sys

folder = sys.argv[1]
files = sorted(glob.glob(os.path.join(folder, "*.json")))
data = []
for path in files:
	with open(path, encoding="utf-8") as f:
		data.append(json.load(f))
ok = len(files) >= 2 and all(
	d.get("schemaVersion") == 1 and d.get("id", "") + ".json" == os.path.basename(path) for d, path in zip(data, files)
)
print(
	"SHELL %s conversations saved as JSON after exit | %d files: %s"
	% ("PASS" if ok else "FAIL", len(files), [(d.get("title"), len(d.get("messages", []))) for d in data]),
)
leftovers = [name for name in os.listdir(folder) if name.startswith(".tmp")] if os.path.isdir(folder) else []
print("SHELL %s no temporary conversation files left | %r" % ("FAIL" if leftovers else "PASS", leftovers))
