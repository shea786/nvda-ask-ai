#!/usr/bin/env python3
"""Build askGPT-<version>.nvda-addon from the addon/ folder using only the standard library."""

import configparser
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
ADDON_DIR = os.path.join(ROOT, "addon")
EXCLUDED_DIRS = {"__pycache__"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo")


def readManifest():
	parser = configparser.ConfigParser(interpolation=None)
	with open(os.path.join(ADDON_DIR, "manifest.ini"), encoding="utf-8") as f:
		parser.read_string("[manifest]\n" + f.read())
	manifest = parser["manifest"]
	return manifest["name"].strip().strip('"'), manifest["version"].strip().strip('"')


def iterAddonFiles():
	for dirPath, dirNames, fileNames in os.walk(ADDON_DIR):
		dirNames[:] = sorted(d for d in dirNames if d not in EXCLUDED_DIRS)
		for fileName in sorted(fileNames):
			if fileName.endswith(EXCLUDED_SUFFIXES):
				continue
			fullPath = os.path.join(dirPath, fileName)
			yield fullPath, os.path.relpath(fullPath, ADDON_DIR).replace(os.sep, "/")


def build(outputDir=ROOT):
	name, version = readManifest()
	outputPath = os.path.join(outputDir, "%s-%s.nvda-addon" % (name, version))
	with zipfile.ZipFile(outputPath, "w", zipfile.ZIP_DEFLATED) as archive:
		for fullPath, archiveName in iterAddonFiles():
			archive.write(fullPath, archiveName)
	return outputPath


if __name__ == "__main__":
	path = build(sys.argv[1] if len(sys.argv) > 1 else ROOT)
	print("Built %s" % path)
