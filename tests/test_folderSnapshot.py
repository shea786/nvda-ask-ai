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

	def test_threeLevelsAndHiddenFiles(self):
		# L7: three folder levels, hidden files and folders included (the AI may change .env or .git files).
		self.write("a.txt")
		self.write("sub", "b.txt")
		self.write("sub", "deeper", "c.txt")
		self.write("sub", "deeper", "deepest", "d.txt")
		self.write(".git", "config")
		self.write(".hidden.txt")
		snapshot = folderSnapshot.snapshot(self.folder)
		self.assertEqual(
			sorted(snapshot),
			sorted([
				"a.txt",
				os.path.join("sub", "b.txt"),
				os.path.join("sub", "deeper", "c.txt"),
				os.path.join(".git", "config"),
				".hidden.txt",
			]),
		)
		self.assertFalse(snapshot.truncated)

	def test_symlinksAreNotFollowed(self):
		outside = tempfile.mkdtemp(prefix="askai-outside-")
		self.addCleanup(shutil.rmtree, outside, True)
		with open(os.path.join(outside, "secret.txt"), "w") as f:
			f.write("x")
		try:
			os.symlink(outside, os.path.join(self.folder, "link"))
		except (OSError, NotImplementedError):
			self.skipTest("no symlinks here")
		self.assertEqual(sorted(folderSnapshot.snapshot(self.folder)), [])

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
		snapshot = folderSnapshot.snapshot(self.folder, maxEntries=10)
		self.assertEqual(len(snapshot), 10)
		self.assertTrue(snapshot.truncated)
		self.assertTrue(folderSnapshot.isTruncated({}, snapshot))
		self.assertEqual(folderSnapshot.MAX_ENTRIES, 5000)

	def test_missingFolder(self):
		self.assertEqual(folderSnapshot.snapshot(os.path.join(self.folder, "gone")), {})
		self.assertEqual(folderSnapshot.snapshot(""), {})

	def test_summarize(self):
		self.assertEqual(folderSnapshot.summarize(["a", "b"]), (["a", "b"], 0))
		self.assertEqual(folderSnapshot.summarize(list("abcdefgh")), (list("abcde"), 3))


if __name__ == "__main__":
	unittest.main()
