# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: notice files that the AI created or changed in the project folder.
# Pure standard library, no NVDA imports.

import os

MAX_ENTRIES = 5000
# The project folder itself is level 1; files up to three folder levels down are compared.
MAX_DEPTH = 3
SHOWN_NAMES = 5


class Snapshot(dict):
	"""{relative path: (size, mtime)}; truncated is True when the folder had more than maxEntries files."""

	truncated = False


def snapshot(folder, maxEntries=MAX_ENTRIES, maxDepth=MAX_DEPTH):
	"""Files in folder and its subfolders down to maxDepth levels, hidden files (".git", ".env") included.
	Symbolic links and junctions are not followed. At most maxEntries files are recorded; when there are
	more, the result is marked truncated (the caller then says "many files changed").
	"""
	result = Snapshot()

	def scan(directory, prefix, depth):
		try:
			entries = sorted(os.scandir(directory), key=lambda entry: entry.name.lower())
		except OSError:
			return
		for entry in entries:
			if len(result) >= maxEntries:
				result.truncated = True
				return
			name = prefix + entry.name
			try:
				if entry.is_symlink():
					continue
				if entry.is_file(follow_symlinks=False):
					info = entry.stat(follow_symlinks=False)
					result[name] = (info.st_size, info.st_mtime)
				elif depth < maxDepth and entry.is_dir(follow_symlinks=False):
					scan(entry.path, name + os.sep, depth + 1)
			except OSError:
				continue

	if folder:
		scan(folder, "", 1)
	return result


def compare(before, after):
	"""(created, changed): sorted relative paths of new files and of files whose size or time changed."""
	created = sorted((name for name in after if name not in before), key=str.lower)
	changed = sorted((name for name in after if name in before and after[name] != before[name]), key=str.lower)
	return created, changed


def isTruncated(*snapshots):
	return any(getattr(item, "truncated", False) for item in snapshots)


def summarize(names, shown=SHOWN_NAMES):
	"""(first names, how many more) for announcing a list without reading hundreds of names."""
	return list(names[:shown]), max(0, len(names) - shown)
