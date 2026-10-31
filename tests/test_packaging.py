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
		for name in ("openaiClient.py", "credentials.py", "recorder.py", "codexClient.py", "modelCatalog.py"):
			self.assertFalse(os.path.exists(os.path.join(helpers.PACKAGE_DIR, name)), name)
		self.assertFalse(os.path.exists(os.path.join(ROOT, "addon", "globalPlugins", "askGPT")))

	def test_pureModulesHaveNoNvdaImports(self):
		for name in (
			"providers/__init__.py",
			"providers/base.py",
			"providers/codex.py",
			"providers/claude.py",
			"providers/modelCatalog.py",
			"textUtils.py",
			"conversations.py",
			"dictation.py",
			"exporters.py",
			"folderSnapshot.py",
			"migration.py",
			"updater.py",
			"search.py",
			"history.py",
			"status.py",
			"context.py",
			"png.py",
			"attachments.py",
			"screenshot.py",
		):
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
			# Ask AI 1.2: Claude setup and sign-in, the honest Windows note, updates and the licence.
			"irm https://claude.ai/install.ps1 | iex",
			"npm install -g @anthropic-ai/claude-code",
			"claude update",
			"Enter sign-in code",
			"no operating-system sandbox on Windows",
			"Save answer as",
			"Check for Ask AI updates",
			"Add-on Store",
			"GNU General Public License version 2",
			"%LOCALAPPDATA%\\askAI\\conversations",
			# Round 6: shortcuts table and the new features.
			"<h2>Keyboard shortcuts</h2>",
			"NVDA+Alt+Shift+S",
			"NVDA+Alt+A",
			"Ctrl+F",
			"Shift+F3",
			"Ctrl+S",
			"Can't use protected or password fields",
			"<strong>Include a screenshot of</strong>",
			# Round 7: tab order, options, what is attached, security and privacy, signing, full status list.
			"<h2>What is attached, and when it is cleared</h2>",
			"<h2>Security and privacy</h2>",
			"Conversation options",
			"Still attached: 2 files",
			"--ignore-user-config",
			"safe mode",
			"valid signature",
			"Not signed in to Claude.",
			"Screenshot looks blank, possibly because Screen Curtain is on",
			"Allow the AI to create and change files in",
			"Long text attached as a file",
			"history.json",
		):
			self.assertIn(required, html)
		self.assertNotIn("askGPT-1.1.0", html)

	def test_repositoryDocs(self):
		for name in ("README.md", "RELEASING.md", "LICENSE", os.path.join("tester", "TESTING.txt")):
			self.assertTrue(os.path.isfile(os.path.join(ROOT, name)), name)
		with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
			readme = f.read()
		self.assertIn("GNU General Public License version 2", readme)
		self.assertIn("askAI-1.2.0.nvda-addon", readme)
		with open(os.path.join(ROOT, "tester", "TESTING.txt"), encoding="utf-8") as f:
			testing = f.read()
		self.assertIn("askAI-1.2.0.nvda-addon", testing)
		self.assertIn("Claude", testing)
		with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as f:
			license = f.read()
		with open(os.path.join(ROOT, "addon", "copying.txt"), encoding="utf-8") as f:
			self.assertEqual(f.read(), license)
		self.assertIn("GNU GENERAL PUBLIC LICENSE", license)
		self.assertIn("Version 2, June 1991", license)

	def test_workflows(self):
		with open(os.path.join(ROOT, ".github", "workflows", "release.yml"), encoding="utf-8") as f:
			release = f.read()
		for required in ('"v*"', "contents: write", "python-version: \"3.11\"", "unittest discover", "build.py",
				"SHA256SUMS.txt", "TESTING.txt", "--prerelease", "GITHUB_TOKEN", "actions/checkout@11d5960a", "actions/setup-python@a26af69b", "persist-credentials: false", "environment: release", "SHA256SUMS.txt.sig"):
			self.assertIn(required, release)
		self.assertTrue(os.path.isfile(os.path.join(ROOT, ".github", "workflows", "ci.yml")))


class TestManifest(unittest.TestCase):
	def test_manifestFields(self):
		parser = configparser.ConfigParser(interpolation=None)
		with open(os.path.join(ROOT, "addon", "manifest.ini"), encoding="utf-8") as f:
			parser.read_string("[m]\n" + f.read())
		manifest = parser["m"]
		self.assertEqual(manifest["name"], "askAI")
		self.assertEqual(manifest["summary"], '"Ask AI"')
		self.assertEqual(manifest["version"], "1.2.0")
		self.assertEqual(manifest["author"], '"Asif Ali"')
		self.assertEqual(manifest["url"], '"https://github.com/shea786/nvda-ask-gpt"')
		self.assertIn("ChatGPT", manifest["description"])
		self.assertIn("Claude", manifest["description"])
		self.assertEqual(manifest["minimumNVDAVersion"], "2024.1")
		self.assertEqual(manifest["lastTestedNVDAVersion"], "2026.1")


class TestBuild(unittest.TestCase):
	def test_buildProducesValidAddon(self):
		spec = importlib.util.spec_from_file_location("askAIBuild", os.path.join(ROOT, "build.py"))
		build = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(build)
		outDir = tempfile.mkdtemp()
		self.addCleanup(shutil.rmtree, outDir, True)
		# A stray cache directory must not end up in the package.
		cacheDir = os.path.join(helpers.PACKAGE_DIR, "__pycache__")
		os.makedirs(cacheDir, exist_ok=True)
		path = build.build(outDir)
		self.assertEqual(os.path.basename(path), "askAI-1.2.0.nvda-addon")
		with zipfile.ZipFile(path) as archive:
			names = archive.namelist()
			self.assertIsNone(archive.testzip())
		self.assertIn("manifest.ini", names)
		for module in (
			"__init__",
			"conversationManager",
			"conversations",
			"dialogs",
			"dictation",
			"exporters",
			"folderSnapshot",
			"migration",
			"search",
			"history",
			"status",
			"context",
			"png",
			"attachments",
			"screenshot",
			"textUtils",
			"updateCheck",
			"updater",
			"providers/__init__",
			"providers/base",
			"providers/codex",
			"providers/claude",
			"providers/modelCatalog",
		):
			self.assertIn("globalPlugins/askAI/%s.py" % module, names)
		self.assertIn("doc/en/readme.html", names)
		self.assertIn("copying.txt", names)
		with zipfile.ZipFile(path) as archive:
			self.assertTrue(archive.read("copying.txt").decode("utf-8").lstrip().startswith("GNU GENERAL PUBLIC LICENSE"))
		self.assertFalse([n for n in names if "askGPT" in n])
		self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc")])
		self.assertFalse([n for n in names if n.startswith("addon/")])


if __name__ == "__main__":
	unittest.main()
