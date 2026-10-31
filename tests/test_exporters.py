"""Feature 2: built-in Word, PDF, text, HTML and Markdown export without Office or extra libraries."""

import io
import os
import re
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

import helpers  # noqa: F401  (adds the add-on package to sys.path)

import exporters

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
BLOCKS = [
	("heading", "GPT"),
	("text", "First line <with> & \"quotes\"\nSecond line\twith tab\n\nCafé – 日本"),
]


class TestFileNames(unittest.TestCase):
	def test_sanitize(self):
		self.assertEqual(exporters.sanitizeFileName('What is 2/3 <of> "pi"?'), "What is 2 3 of pi")
		self.assertEqual(exporters.sanitizeFileName("  trailing dots... "), "trailing dots")
		self.assertEqual(exporters.sanitizeFileName("CON"), "_CON")
		self.assertEqual(exporters.sanitizeFileName("lpt1.txt"), "_lpt1.txt")
		self.assertEqual(exporters.sanitizeFileName("\x00\x1f:*?"), "Answer")
		self.assertEqual(exporters.sanitizeFileName("", fallback="Chat"), "Chat")
		long = exporters.sanitizeFileName("word " * 40)
		self.assertLessEqual(len(long), exporters.MAX_BASE_NAME)
		self.assertFalse(long.endswith(" "))

	def test_fileNameFromQuestion(self):
		self.assertEqual(exporters.fileNameFromText("Write a letter to my landlord\nabout the heating"), "Write a letter to my landlord")
		self.assertEqual(exporters.fileNameFromText(""), "Answer")
		self.assertLessEqual(len(exporters.fileNameFromText("x" * 200)), 60)

	def test_uniquePathAddsNumbers(self):
		existing = {os.path.join("f", "Report.docx"), os.path.join("f", "Report (2).docx")}
		self.assertEqual(
			exporters.uniquePath("f", "Report", ".docx", exists=lambda p: p in existing),
			os.path.join("f", "Report (3).docx"),
		)
		self.assertEqual(exporters.uniquePath("f", "New", ".pdf", exists=lambda p: False), os.path.join("f", "New.pdf"))


class TestWriting(unittest.TestCase):
	def setUp(self):
		self.folder = tempfile.mkdtemp(prefix="askgpt-export-")
		self.addCleanup(shutil.rmtree, self.folder, True)

	def test_neverOverwrites(self):
		first = exporters.export(self.folder, "Answer", exporters.FORMAT_TXT, "T", [("text", "one")])
		second = exporters.export(self.folder, "Answer", exporters.FORMAT_TXT, "T", [("text", "two")])
		self.assertEqual(os.path.basename(first), "Answer.txt")
		self.assertEqual(os.path.basename(second), "Answer (2).txt")
		with open(first, encoding="utf-8-sig") as f:
			self.assertIn("one", f.read())

	def test_createsFolder(self):
		target = os.path.join(self.folder, "Documents", "Ask AI")
		path = exporters.export(target, "x", exporters.FORMAT_MD, "T", [("text", "plain")], [("text", "**raw**")])
		with open(path, encoding="utf-8") as f:
			self.assertIn("**raw**", f.read(), "Markdown keeps the raw answer")

	def test_defaultFolder(self):
		self.assertEqual(exporters.defaultSaveFolder(os.path.join("C", "Docs")), os.path.join("C", "Docs", "Ask AI"))


class TestFormats(unittest.TestCase):
	def test_text(self):
		data = exporters.renderText("Title", BLOCKS)
		self.assertTrue(data.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM for Notepad")
		text = data.decode("utf-8-sig")
		self.assertIn("Title\r\n", text)
		self.assertIn("Café – 日本", text)

	def test_html(self):
		page = exporters.renderHtml("A <b> title", BLOCKS).decode("utf-8")
		self.assertIn("<h1>A &lt;b&gt; title</h1>", page)
		self.assertIn("<h2>GPT</h2>", page)
		self.assertIn("<p>First line &lt;with&gt; &amp; &quot;quotes&quot;</p>", page)
		self.assertIn('<meta charset="utf-8">', page)

	def test_markdown(self):
		text = exporters.renderMarkdown("T", BLOCKS).decode("utf-8")
		self.assertTrue(text.startswith("# T\n"))
		self.assertIn("## GPT", text)

	def test_docxIsAValidPackage(self):
		data = exporters.renderDocx("Title & more", BLOCKS + [("text", "bad \x01 control \x0b chars")])
		with zipfile.ZipFile(io.BytesIO(data)) as package:
			names = set(package.namelist())
			self.assertTrue(
				{"[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/_rels/document.xml.rels", "word/styles.xml", "docProps/core.xml"}
				<= names,
			)
			for name in names:
				ET.fromstring(package.read(name))  # Every part is well-formed XML.
			types = package.read("[Content_Types].xml").decode("utf-8")
			self.assertIn("wordprocessingml.document.main+xml", types)
			rels = ET.fromstring(package.read("_rels/.rels"))
			self.assertIn("word/document.xml", [r.get("Target") for r in rels])
			document = ET.fromstring(package.read("word/document.xml"))
		paragraphs = document.findall(".//%sp" % W)
		texts = ["".join(t.text or "" for t in p.iter(W + "t")) for p in paragraphs]
		styles = [
			(p.find("%spPr/%spStyle" % (W, W)).get(W + "val") if p.find("%spPr/%spStyle" % (W, W)) is not None else None)
			for p in paragraphs
		]
		self.assertEqual(texts[0], "Title & more")
		self.assertEqual(styles[0], "Heading1")
		self.assertEqual((texts[1], styles[1]), ("GPT", "Heading2"))
		self.assertEqual(texts[2], 'First line <with> & "quotes"', "one paragraph per line, escaped")
		self.assertEqual(texts[3], "Second line    with tab")
		self.assertEqual(texts[4], "", "empty lines stay as empty paragraphs")
		self.assertEqual(texts[5], "Café – 日本", "UTF-8 text survives")
		self.assertEqual(texts[6], "bad  control  chars", "characters XML forbids are dropped")

	def _parsePdf(self, data):
		self.assertTrue(data.startswith(b"%PDF-1.4\n"))
		self.assertTrue(data.rstrip().endswith(b"%%EOF"))
		startxref = int(re.search(rb"startxref\n(\d+)\n%%EOF", data).group(1))
		self.assertEqual(data[startxref:startxref + 4], b"xref")
		header = re.match(rb"xref\n0 (\d+)\n", data[startxref:])
		count = int(header.group(1))
		entries = data[startxref + header.end():].split(b"\n")[:count]
		self.assertEqual(entries[0], b"0000000000 65535 f ")
		for number, entry in enumerate(entries[1:], start=1):
			offset = int(entry[:10])
			self.assertTrue(entry.endswith(b" 00000 n "), entry)
			self.assertTrue(data[offset:].startswith(b"%d 0 obj" % number), "xref offset of object %d" % number)
		trailer = data[data.index(b"trailer"):]
		self.assertIn(b"/Size %d" % count, trailer)
		self.assertIn(b"/Root 1 0 R", trailer)
		for match in re.finditer(rb"<< /Length (\d+) >>\nstream\n", data):
			length = int(match.group(1))
			self.assertEqual(data[match.end() + length:match.end() + length + 10], b"\nendstream")
		return data

	def test_pdfStructureAndText(self):
		data = self._parsePdf(exporters.renderPdf("My (title)", BLOCKS))
		self.assertIn(b"(My \\(title\\)) Tj", data)
		self.assertIn(b"(First line <with> & \"quotes\") Tj", data)
		self.assertIn(b"/BaseFont /Helvetica", data)
		self.assertIn(b"/Encoding /WinAnsiEncoding", data)
		# e acute is in Windows-1252 (octal 351); Japanese is not and becomes "?".
		self.assertIn(b"(Caf\\351 \\226 ??) Tj", data)
		self.assertIn(b"/Count 1", data)

	def test_pdfWrapsAndPaginates(self):
		longText = "\n".join("Line %d %s" % (i, "word " * 30) for i in range(120))
		data = self._parsePdf(exporters.renderPdf("Long", [("text", longText)]))
		pages = int(re.search(rb"/Count (\d+)", data).group(1))
		self.assertGreater(pages, 3)
		self.assertEqual(data.count(b"/Type /Page "), pages)
		usable = exporters.PAGE_WIDTH - 2 * exporters.MARGIN
		for line in re.findall(rb"\((.*?)\) Tj", data):
			self.assertLessEqual(exporters.textWidth(line, exporters.HEADING_SIZE if line == b"Long" else exporters.FONT_SIZE), usable)

	def test_wrapBreaksLongWords(self):
		lines = exporters.wrapLine(b"x" * 500, 11, 100)
		self.assertGreater(len(lines), 1)
		self.assertEqual(b"".join(lines), b"x" * 500)
		for line in lines:
			self.assertLessEqual(exporters.textWidth(line, 11), 100)

	def test_emptyPdfIsValid(self):
		self._parsePdf(exporters.renderPdf("", []))

	def test_renderAllFormats(self):
		for key, extension in exporters.FORMATS:
			self.assertTrue(exporters.render(key, "T", BLOCKS), key)
			self.assertTrue(extension.startswith("."))


if __name__ == "__main__":
	unittest.main()
