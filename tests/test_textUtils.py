"""Tests for the Markdown stripper used before speaking answers."""

import unittest

import helpers  # noqa: F401  (adds the add-on package to sys.path)

from textUtils import formatTurn, stripMarkdown


class TestStripMarkdown(unittest.TestCase):
	def test_emptyAndPlain(self):
		self.assertEqual(stripMarkdown(""), "")
		self.assertEqual(stripMarkdown(None), "")
		self.assertEqual(stripMarkdown("Just a sentence."), "Just a sentence.")

	def test_emphasis(self):
		self.assertEqual(stripMarkdown("This is **bold** and __also bold__."), "This is bold and also bold.")
		self.assertEqual(stripMarkdown("This is *italic* and _also_."), "This is italic and also.")
		self.assertEqual(stripMarkdown("~~gone~~ text"), "gone text")

	def test_headings(self):
		self.assertEqual(stripMarkdown("# Title\n## Sub title ##\nBody"), "Title\nSub title\nBody")

	def test_codeBlocksKeepContent(self):
		text = "Run this:\n```python\nprint('hi')\n```\nDone, use `pip list`."
		self.assertEqual(stripMarkdown(text), "Run this:\nprint('hi')\nDone, use pip list.")

	def test_linksAndImages(self):
		self.assertEqual(
			stripMarkdown("See [NV Access](https://nvaccess.org) and ![logo](a.png)."),
			"See NV Access (https://nvaccess.org) and logo.",
		)

	def test_listsQuotesAndRules(self):
		text = "- one\n* two\n+ three\n1. four\n> quoted\n---\nend"
		self.assertEqual(stripMarkdown(text), "one\ntwo\nthree\n1. four\nquoted\n\nend")

	def test_tables(self):
		text = "| Name | Age |\n|------|-----|\n| Ann | 30 |"
		self.assertEqual(stripMarkdown(text), "Name, Age\nAnn, 30")

	def test_doesNotMangleOrdinaryText(self):
		self.assertEqual(stripMarkdown("2 * 3 * 4 = 24"), "2 * 3 * 4 = 24")
		self.assertEqual(stripMarkdown("file_name_here and snake_case"), "file_name_here and snake_case")
		self.assertEqual(stripMarkdown("C# and F# are languages"), "C# and F# are languages")
		self.assertEqual(stripMarkdown("Price - 5 dollars"), "Price - 5 dollars")

	def test_collapsesBlankLines(self):
		self.assertEqual(stripMarkdown("a\r\n\r\n\r\n\r\nb"), "a\n\nb")


class TestFormatTurn(unittest.TestCase):
	def test_format(self):
		self.assertEqual(formatTurn("You", "  hello \n"), "You: hello")


if __name__ == "__main__":
	unittest.main()
