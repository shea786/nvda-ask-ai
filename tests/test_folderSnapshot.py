"""Feature 2: noticing files GPT created or changed in the project folder."""

import os
import shutil
import tempfile
import time
import unittest

import helpers  # noqa: F401  (adds the add-on package to sys.path)

import folderSnapshot


class TestFolderSnapshot(unittest.TestCase):
	def setUp(self):
		self.folder = tempfile.mkdtemp(prefix="askgpt-snap-")
		self.addCleanup(shutil.rmtree, self.folder, True)

	def write(self, *parts, text="x"):
		path = os.path.join(self.folder, *parts)
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(path, "w") as f:
			f.write(text)
		return path

	def test_topLevelAndOneLevelDeep(self):
		self.write("a.txt")
		self.write("sub", "b.txt")
		self.write("sub", "deeper", "c.txt")
		self.write(".git", "config")
		self.write(".hidden.txt")
		snapshot = folderSnapshot.snapshot(self.folder)
		self.assertEqual(sorted(snapshot), ["a.txt", os.path.join("sub", "b.txt")])

	def test_compare(self):
		self.write("keep.txt")
		path = self.write("change.txt")
		before = folderSnapshot.snapshot(self.folder)
		self.write("new.docx")
		self.write("out", "report.pdf")
		with open(path, "w") as f:
			f.write("longer text")
		os.utime(path, (time.time() + 5, time.time() + 5))
		created, changed = folderSnapshot.compare(before, folderSnapshot.snapshot(self.folder))
		self.assertEqual(created, ["new.docx", os.path.join("out", "report.pdf")])
		self.assertEqual(changed, ["change.txt"])

	def test_cap(self):
		for i in range(30):
			self.write("f%02d.txt" % i)
		self.assertEqual(len(folderSnapshot.snapshot(self.folder, maxEntries=10)), 10)

	def test_missingFolder(self):
		self.assertEqual(folderSnapshot.snapshot(os.path.join(self.folder, "gone")), {})
		self.assertEqual(folderSnapshot.snapshot(""), {})

	def test_summarize(self):
		self.assertEqual(folderSnapshot.summarize(["a", "b"]), (["a", "b"], 0))
		self.assertEqual(folderSnapshot.summarize(list("abcdefgh")), (list("abcde"), 3))


if __name__ == "__main__":
	unittest.main()
