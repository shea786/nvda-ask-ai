"""Feature 3: the model list comes from Codex's own models_cache.json."""

import json
import os
import shutil
import tempfile
import unittest

import helpers  # noqa: F401  (adds the add-on package to sys.path)

from providers import modelCatalog
from providers.modelCatalog import ModelInfo

# Shaped like ~/.codex/models_cache.json from codex-cli 0.160.0 (extra fields trimmed).
CACHE = {
	"fetched_at": "2026-10-31T01:48:36Z",
	"client_version": "0.160.0",
	"identity": "abc",
	"models": [
		{
			"slug": "gpt-6.1-sol",
			"display_name": "GPT 6.1 Sol",
			"visibility": "list",
			"supported_in_api": True,
			"default_reasoning_level": "low",
			"supported_reasoning_levels": [
				{"effort": "low", "description": "Fast"},
				{"effort": "medium", "description": "Balanced"},
				{"effort": "high", "description": "Deep"},
				{"effort": "xhigh", "description": "Deeper"},
			],
		},
		{
			"slug": "gpt-6-luna",
			"display_name": "GPT 6.0 Luna",
			"visibility": "list",
			"default_reasoning_level": "medium",
			"supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}, {"effort": "bad effort!"}],
		},
		{"slug": "codex-auto-review", "display_name": "Codex Auto Review", "visibility": "hide"},
		{"slug": "gpt-6.1-sol", "display_name": "Duplicate", "visibility": "list"},
		{"slug": "no-levels", "visibility": "list", "default_reasoning_level": "ultra"},
		{"display_name": "No slug", "visibility": "list"},
		"junk",
	],
}


class TestModelCatalog(unittest.TestCase):
	def setUp(self):
		self.folder = tempfile.mkdtemp(prefix="askgpt-models-")
		self.addCleanup(shutil.rmtree, self.folder, True)

	def write(self, content):
		path = os.path.join(self.folder, "models_cache.json")
		with open(path, "w", encoding="utf-8") as f:
			f.write(content if isinstance(content, str) else json.dumps(content))
		return path

	def test_parse(self):
		models = modelCatalog.loadModels(self.write(CACHE))
		self.assertEqual([m.slug for m in models], ["gpt-6.1-sol", "gpt-6-luna", "no-levels"], "only visibility=list, no duplicates")
		self.assertEqual(models[0], ModelInfo("gpt-6.1-sol", "GPT 6.1 Sol", ("low", "medium", "high", "xhigh"), "low"))
		self.assertEqual(models[1].efforts, ("low", "medium"), "unsafe effort names are dropped")
		self.assertEqual(models[2].displayName, "no-levels")
		self.assertEqual(models[2].efforts, modelCatalog.DEFAULT_EFFORTS)
		self.assertEqual(models[2].defaultEffort, "low", "a default the model does not list falls back")

	def test_missingOrBrokenFile(self):
		self.assertEqual(modelCatalog.loadModels(os.path.join(self.folder, "missing.json")), [])
		self.assertEqual(modelCatalog.loadModels(self.write("{broken")), [])
		self.assertEqual(modelCatalog.loadModels(self.write({"models": "nope"})), [])
		self.assertEqual(modelCatalog.loadModels(self.write([1, 2])), [])

	def test_cachePath(self):
		self.assertEqual(modelCatalog.modelsCachePath({"CODEX_HOME": "/ch"}), os.path.join("/ch", "models_cache.json"))
		self.assertEqual(
			modelCatalog.modelsCachePath({"USERPROFILE": "/users/jo"}),
			os.path.join("/users/jo", ".codex", "models_cache.json"),
		)
		self.assertEqual(modelCatalog.modelsCachePath({"HOME": "/home/jo"}), os.path.join("/home/jo", ".codex", "models_cache.json"))
		self.assertEqual(modelCatalog.loadModels(env={"CODEX_HOME": self.folder}), [])
		self.write(CACHE)
		self.assertEqual(len(modelCatalog.loadModels(env={"CODEX_HOME": self.folder})), 3)

	def test_choicesAndEfforts(self):
		models = modelCatalog.parseModels(CACHE)
		choices = modelCatalog.modelChoices(models, extraSlugs=["", "gpt-6.1-sol", "my-custom-model"])
		self.assertEqual([m.slug for m in choices], ["gpt-6.1-sol", "gpt-6-luna", "no-levels", "my-custom-model"])
		self.assertEqual(modelCatalog.effortsFor(models, "gpt-6.1-sol"), ("low", "medium", "high", "xhigh"))
		self.assertEqual(modelCatalog.effortsFor(models, ""), modelCatalog.DEFAULT_EFFORTS)
		self.assertEqual(modelCatalog.effortsFor(models, "unknown"), modelCatalog.DEFAULT_EFFORTS)
		self.assertEqual(modelCatalog.pickEffort(models, "gpt-6.1-sol", "xhigh"), "xhigh")
		self.assertEqual(modelCatalog.pickEffort(models, "gpt-6-luna", "xhigh"), "medium", "model default")
		self.assertEqual(modelCatalog.pickEffort(models, "", "high"), "high")
		self.assertEqual(modelCatalog.pickEffort(models, "", "xhigh"), "low")

	def test_comboLabels(self):
		models = modelCatalog.parseModels(CACHE)
		self.assertEqual(modelCatalog.comboLabel(models[0]), "GPT 6.1 Sol (gpt-6.1-sol)")
		self.assertEqual(modelCatalog.comboLabel(models[2]), "no-levels")
		self.assertEqual(modelCatalog.slugFromComboText("GPT 6.1 Sol (gpt-6.1-sol)", models), "gpt-6.1-sol")
		self.assertEqual(modelCatalog.slugFromComboText("gpt 6.0 luna", models), "gpt-6-luna")
		self.assertEqual(modelCatalog.slugFromComboText(" my-model ", models), "my-model")
		self.assertEqual(modelCatalog.slugFromComboText("", models), "")

	def test_realLocalCacheIfPresent(self):
		path = modelCatalog.modelsCachePath()
		if not os.path.isfile(path):
			self.skipTest("no local Codex model cache")
		models = modelCatalog.loadModels(path)
		self.assertTrue(models)
		for model in models:
			self.assertTrue(model.slug)
			self.assertTrue(model.efforts)
			self.assertIn(model.defaultEffort, model.efforts)


if __name__ == "__main__":
	unittest.main()
