# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: notice files that the AI created or changed in the project folder.
# Pure standard library, no NVDA imports.

import os

MAX_ENTRIES = 2000
SHOWN_NAMES = 5


def snapshot(folder, maxEntries=MAX_ENTRIES):
	"""{relative path: (size, mtime)} for files in folder and one level of subfolders.
	Hidden entries (starting with ".") are skipped; at most maxEntries files are recorded.
	"""
	result = {}

	def scan(directory, prefix, descend):
		try:
			entries = sorted(os.scandir(directory), key=lambda entry: entry.name.lower())
		except OSError:
			return
		for entry in entries:
			if len(result) >= maxEntries:
				return
			if entry.name.startswith("."):
				continue
			name = prefix + entry.name
			try:
				if entry.is_file():
					info = entry.stat()
					result[name] = (info.st_size, info.st_mtime)
				elif descend and entry.is_dir():
					scan(entry.path, name + os.sep, False)
			except OSError:
				continue

	if folder:
		scan(folder, "", True)
	return result


def compare(before, after):
	"""(created, changed): sorted relative paths of new files and of files whose size or time changed."""
	created = sorted((name for name in after if name not in before), key=str.lower)
	changed = sorted((name for name in after if name in before and after[name] != before[name]), key=str.lower)
	return created, changed


def summarize(names, shown=SHOWN_NAMES):
	"""(first names, how many more) for announcing a list without reading hundreds of names."""
	return list(names[:shown]), max(0, len(names) - shown)
