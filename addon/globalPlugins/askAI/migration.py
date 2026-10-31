# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: bring settings and saved conversations over from the Ask GPT 1.x add-on.
# Pure standard library, no NVDA imports.

import glob
import os
import shutil

OLD_ADDON_NAME = "askGPT"
OLD_CONFIG_SECTION = "askGPT"
OLD_DATA_FOLDER = "askGPT"
NEW_DATA_FOLDER = "askAI"

# Settings Ask GPT 1.1 had, with the same names and meanings in Ask AI.
MIGRATED_KEYS = (
	"codexPath",
	"model",
	"reasoningEffort",
	"instructions",
	"speakAnswers",
	"keepContext",
	"beepWhileWaiting",
	"allowProjectWrites",
	"conversationsDir",
	"dictationSilenceTimeout",
	"timeout",
)


def _toBool(value):
	if isinstance(value, bool):
		return value
	text = str(value).strip().lower()
	if text in ("true", "yes", "on", "1"):
		return True
	if text in ("false", "no", "off", "0"):
		return False
	raise ValueError("not a boolean: %r" % (value,))


def convertValue(value, default):
	"""value (a string from nvda.ini, or already typed) converted to the type of default. Raises ValueError."""
	if isinstance(default, bool):
		return _toBool(value)
	if isinstance(default, int):
		return int(str(value).strip())
	if isinstance(default, float):
		return float(str(value).strip())
	if isinstance(value, (list, tuple)):
		raise ValueError("not a string")
	return str(value)


def migratedSettings(oldSection, defaults, keys=MIGRATED_KEYS):
	"""{key: typed value} to copy from the old section: keys that exist there, convert cleanly and differ
	from the default. Values that cannot be converted are skipped (the new default stays).
	@param oldSection: mapping read from the base profile's [askGPT] section (strings or typed values).
	@param defaults: {key: default value} of the new section, which decides each value's type.
	"""
	result = {}
	if not oldSection:
		return result
	for key in keys:
		if key not in oldSection or key not in defaults:
			continue
		try:
			value = convertValue(oldSection[key], defaults[key])
		except (TypeError, ValueError):
			continue
		if value != defaults[key]:
			result[key] = value
	return result


def oldConversationsDir(env=None):
	"""%LOCALAPPDATA%\\askGPT\\conversations, or None when LOCALAPPDATA is not set."""
	env = os.environ if env is None else env
	localAppData = env.get("LOCALAPPDATA")
	return os.path.join(localAppData, OLD_DATA_FOLDER, "conversations") if localAppData else None


def _jsonFiles(folder):
	if not folder or not os.path.isdir(folder):
		return []
	return sorted(
		path
		for path in glob.glob(os.path.join(glob.escape(folder), "*.json"))
		if not os.path.basename(path).startswith(".") and os.path.isfile(path)
	)


def copyConversations(oldDir, newDir):
	"""Copy (never move) the old conversation files when the new folder has none yet.
	Returns the number of files copied; 0 when there is nothing to do.
	"""
	if not oldDir or not newDir:
		return 0
	if os.path.normcase(os.path.abspath(oldDir)) == os.path.normcase(os.path.abspath(newDir)):
		return 0
	sources = _jsonFiles(oldDir)
	if not sources or _jsonFiles(newDir):
		return 0
	os.makedirs(newDir, exist_ok=True)
	copied = 0
	for source in sources:
		target = os.path.join(newDir, os.path.basename(source))
		if os.path.exists(target):
			continue
		shutil.copy2(source, target)
		copied += 1
	return copied


def findOldAddon(addons, oldName=OLD_ADDON_NAME):
	"""The installed Ask GPT add-on that would still run (enabled, not already being removed), or None.
	@param addons: addonHandler.getAvailableAddons() (objects with name, isEnabled/isDisabled, isPendingRemove).
	"""
	for addon in addons:
		try:
			if getattr(addon, "name", None) != oldName:
				continue
			if getattr(addon, "isPendingRemove", False):
				continue
			enabled = getattr(addon, "isEnabled", None)
			if enabled is None:
				enabled = not getattr(addon, "isDisabled", False)
			if enabled:
				return addon
		except Exception:
			continue
	return None
