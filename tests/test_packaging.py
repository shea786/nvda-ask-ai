"""Packaging checks: every file compiles, NVDA-only modules are valid, and build.py makes a correct add-on."""

import configparser
import importlib.util
import os
import py_compile
import shutil
import tempfile
import unittest
import zipfile

import helpers

ROOT = helpers.ROOT


def allPythonFiles():
	for base in ("addon", "tests"):
		for dirPath, dirNames, fileNames in os.walk(os.path.join(ROOT, base)):
			dirNames[:] = [d for d in dirNames if d != "__pycache__"]
			for name in fileNames:
				if name.endswith(".py"):
					yield os.path.join(dirPath, name)
	yield os.path.join(ROOT, "build.py")


class TestCompile(unittest.TestCase):
	def test_pyCompileEverything(self):
		tempDir = tempfile.mkdtemp()
		self.addCleanup(shutil.rmtree, tempDir, True)
		files = list(allPythonFiles())
		self.assertGreaterEqual(len(files), 8)
		for index, path in enumerate(files):
			with self.subTest(path=os.path.relpath(path, ROOT)):
				py_compile.compile(path, cfile=os.path.join(tempDir, "%d.pyc" % index), doraise=True)

	def test_noRemovedModules(self):
		for name in ("openaiClient.py", "credentials.py", "recorder.py"):
			self.assertFalse(os.path.exists(os.path.join(helpers.PACKAGE_DIR, name)), name)

	def test_pureModulesHaveNoNvdaImports(self):
		for name in ("codexClient.py", "textUtils.py"):
			with open(os.path.join(helpers.PACKAGE_DIR, name), encoding="utf-8") as f:
				source = f.read()
			for nvdaModule in ("import wx", "import gui", "import ui", "import addonHandler", "import config"):
				self.assertNotIn(nvdaModule, source, name)

	def test_tabsIndentation(self):
		for path in allPythonFiles():
			with open(path, encoding="utf-8") as f:
				for lineNumber, line in enumerate(f, 1):
					self.assertFalse(line.startswith("    "), "%s:%d uses spaces" % (path, lineNumber))


class TestUserDocs(unittest.TestCase):
	def test_readmeSetupGuidance(self):
		# A8: setup must be followable without sighted help.
		with open(os.path.join(ROOT, "addon", "doc", "en", "readme.html"), encoding="utf-8") as f:
			html = f.read()
		for required in (
			"winget install OpenJS.NodeJS.LTS",
			"codex --version",
			'<a href="https://nodejs.org/">',
			'<a href="https://github.com/openai/codex/releases">',
			"Alt+Tab repeatedly until you hear \"NVDA Settings\"",
			"CAPTCHA",
			"two-factor",
			"%APPDATA%\\npm\\codex.cmd",
			"Copy sign-in link",
			"Press <strong>Control</strong> to stop",
			"twice quickly",
			"Shift+Enter",
		):
			self.assertIn(required, html)


class TestManifest(unittest.TestCase):
	def test_manifestFields(self):
		parser = configparser.ConfigParser(interpolation=None)
		with open(os.path.join(ROOT, "addon", "manifest.ini"), encoding="utf-8") as f:
			parser.read_string("[m]\n" + f.read())
		manifest = parser["m"]
		self.assertEqual(manifest["name"], "askGPT")
		self.assertEqual(manifest["version"], "1.0.0")
		self.assertEqual(manifest["minimumNVDAVersion"], "2024.1")
		self.assertEqual(manifest["lastTestedNVDAVersion"], "2026.1")


class TestBuild(unittest.TestCase):
	def test_buildProducesValidAddon(self):
		spec = importlib.util.spec_from_file_location("askGPTBuild", os.path.join(ROOT, "build.py"))
		build = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(build)
		outDir = tempfile.mkdtemp()
		self.addCleanup(shutil.rmtree, outDir, True)
		# A stray cache directory must not end up in the package.
		cacheDir = os.path.join(helpers.PACKAGE_DIR, "__pycache__")
		os.makedirs(cacheDir, exist_ok=True)
		path = build.build(outDir)
		self.assertEqual(os.path.basename(path), "askGPT-1.0.0.nvda-addon")
		with zipfile.ZipFile(path) as archive:
			names = archive.namelist()
			self.assertIsNone(archive.testzip())
		self.assertIn("manifest.ini", names)
		for module in ("__init__", "codexClient", "dialogs", "textUtils"):
			self.assertIn("globalPlugins/askGPT/%s.py" % module, names)
		self.assertIn("doc/en/readme.html", names)
		self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc")])
		self.assertFalse([n for n in names if n.startswith("addon/")])


if __name__ == "__main__":
	unittest.main()
