# -*- coding: UTF-8 -*-
# Ask GPT NVDA add-on: the list of models Codex offers, read from Codex's own cache file.
# Pure standard library, no NVDA imports.
#
# Codex keeps %CODEX_HOME%\models_cache.json (default %USERPROFILE%\.codex). Observed with codex-cli 0.160.0:
#   {"fetched_at": ..., "client_version": ..., "identity": ...,
#    "models": [{"slug", "display_name", "visibility": "list" | "hide", "default_reasoning_level",
#                "supported_reasoning_levels": [{"effort": "low", "description": ...}, ...], ...}]}
# The file is undocumented, so everything here is defensive.

import json
import os
import re

DEFAULT_EFFORTS = ("low", "medium", "high")
# Reasoning effort names go on the command line (-c model_reasoning_effort=...), so keep them simple.
_SAFE_EFFORT = re.compile(r"^[a-z][a-z0-9_-]{0,19}$")


def isValidEffort(effort):
	return bool(effort) and bool(_SAFE_EFFORT.match(effort))


class ModelInfo:
	def __init__(self, slug, displayName="", efforts=DEFAULT_EFFORTS, defaultEffort=""):
		self.slug = slug
		self.displayName = displayName or slug
		self.efforts = tuple(efforts) or DEFAULT_EFFORTS
		self.defaultEffort = defaultEffort if defaultEffort in self.efforts else self.efforts[0]

	def __repr__(self):
		return "ModelInfo(%r, %r, %r, %r)" % (self.slug, self.displayName, self.efforts, self.defaultEffort)

	def __eq__(self, other):
		return isinstance(other, ModelInfo) and (self.slug, self.displayName, self.efforts, self.defaultEffort) == (
			other.slug,
			other.displayName,
			other.efforts,
			other.defaultEffort,
		)


def codexHome(env=None):
	"""%CODEX_HOME%, else the .codex folder in the user's home (USERPROFILE on Windows, HOME elsewhere)."""
	env = os.environ if env is None else env
	explicit = (env.get("CODEX_HOME") or "").strip()
	if explicit:
		return explicit
	home = env.get("USERPROFILE") or env.get("HOME") or os.path.expanduser("~")
	return os.path.join(home, ".codex")


def modelsCachePath(env=None):
	return os.path.join(codexHome(env), "models_cache.json")


def _efforts(entry):
	levels = entry.get("supported_reasoning_levels")
	result = []
	if isinstance(levels, list):
		for level in levels:
			effort = level.get("effort") if isinstance(level, dict) else level
			if isinstance(effort, str) and isValidEffort(effort) and effort not in result:
				result.append(effort)
	return tuple(result) or DEFAULT_EFFORTS


def parseModels(data):
	"""ModelInfo list from the decoded cache, keeping only models Codex lists for users."""
	models = []
	seen = set()
	entries = data.get("models") if isinstance(data, dict) else None
	if not isinstance(entries, list):
		return models
	for entry in entries:
		if not isinstance(entry, dict):
			continue
		slug = entry.get("slug")
		if not isinstance(slug, str) or not slug or slug in seen:
			continue
		if entry.get("visibility") != "list":
			continue
		displayName = entry.get("display_name")
		defaultEffort = entry.get("default_reasoning_level")
		models.append(
			ModelInfo(
				slug,
				displayName if isinstance(displayName, str) else "",
				_efforts(entry),
				defaultEffort if isinstance(defaultEffort, str) else "",
			),
		)
		seen.add(slug)
	return models


def loadModels(path=None, env=None):
	"""Models from Codex's cache, or [] if the file is missing or unreadable."""
	path = path or modelsCachePath(env)
	try:
		with open(path, "r", encoding="utf-8") as f:
			data = json.load(f)
	except (OSError, ValueError):
		return []
	return parseModels(data)


def findModel(models, slug):
	for model in models:
		if model.slug == slug:
			return model
	return None


def modelChoices(models, extraSlugs=()):
	"""ModelInfo list for a model chooser: cached models, then any configured slug that is not cached.
	The "Default" entry is added by the UI.
	"""
	result = list(models)
	for slug in extraSlugs:
		if slug and not findModel(result, slug):
			result.append(ModelInfo(slug))
	return result


def effortsFor(models, slug):
	model = findModel(models, slug) if slug else None
	return model.efforts if model else DEFAULT_EFFORTS


def pickEffort(models, slug, wanted):
	"""wanted if the model supports it, else the model's default, else the first level."""
	efforts = effortsFor(models, slug)
	if wanted in efforts:
		return wanted
	model = findModel(models, slug) if slug else None
	if model is not None:
		return model.defaultEffort
	return efforts[0]


_LABEL = re.compile(r"\(([^()\s]+)\)\s*$")


def comboLabel(model):
	"""Text for an editable model box: "GPT 6.1 Sol (gpt-6.1-sol)"."""
	if model.displayName and model.displayName != model.slug:
		return "%s (%s)" % (model.displayName, model.slug)
	return model.slug


def slugFromComboText(text, models):
	"""Inverse of comboLabel; also accepts a typed slug or display name. "" means Codex's default."""
	text = (text or "").strip()
	if not text:
		return ""
	match = _LABEL.search(text)
	if match:
		return match.group(1)
	for model in models:
		if text.lower() == model.displayName.lower():
			return model.slug
	return text
