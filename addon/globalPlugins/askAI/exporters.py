# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: save text as Word, PDF, plain text, HTML or Markdown files.
# Pure standard library and no NVDA imports, so it works on any PC without Office or Python packages.
#
# A document is a title plus a list of blocks: ("heading", text) or ("text", text).
# Text blocks may hold several lines; each line becomes a paragraph.

import datetime
import html
import io
import os
import re
import zipfile

FORMAT_DOCX = "docx"
FORMAT_PDF = "pdf"
FORMAT_TXT = "txt"
FORMAT_HTML = "html"
FORMAT_MD = "md"
# (key, file extension), in the order offered to the user.
FORMATS = (
	(FORMAT_DOCX, ".docx"),
	(FORMAT_PDF, ".pdf"),
	(FORMAT_TXT, ".txt"),
	(FORMAT_HTML, ".html"),
	(FORMAT_MD, ".md"),
)
EXTENSIONS = dict(FORMATS)
MAX_BASE_NAME = 80
DEFAULT_BASE_NAME = "Answer"

# File names

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}


def sanitizeFileName(name, fallback=DEFAULT_BASE_NAME, maxLength=MAX_BASE_NAME):
	"""A safe Windows file name (without extension) from free text."""
	name = _INVALID_FILENAME_CHARS.sub(" ", name or "")
	name = re.sub(r"\s+", " ", name).strip()
	if len(name) > maxLength:
		cut = name[:maxLength]
		space = cut.rfind(" ")
		name = cut[:space] if space > maxLength // 2 else cut
	name = name.rstrip(" .")
	if not name:
		name = fallback
	if name.split(".")[0].upper() in _RESERVED_NAMES:
		name = "_" + name
	return name


def fileNameFromText(text, fallback=DEFAULT_BASE_NAME):
	"""Suggested file name from a question: its first line, cut at about 60 characters."""
	firstLine = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
	return sanitizeFileName(firstLine, fallback=fallback, maxLength=60)


def uniquePath(folder, baseName, extension, exists=os.path.exists):
	"""folder/baseName.ext, or "baseName (2).ext", "(3)" ... if that already exists."""
	candidate = os.path.join(folder, baseName + extension)
	number = 2
	while exists(candidate):
		candidate = os.path.join(folder, "%s (%d)%s" % (baseName, number, extension))
		number += 1
	return candidate


def defaultSaveFolder(documentsDir):
	return os.path.join(documentsDir, "Ask AI")


# Plain text helpers


def _lines(blocks):
	for kind, text in blocks:
		yield kind, (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def renderText(title, blocks):
	parts = []
	if title:
		parts.append(title)
		parts.append("")
	for kind, lines in _lines(blocks):
		parts.extend(lines)
		parts.append("")
	# UTF-8 with a byte order mark, so Notepad on older Windows detects the encoding.
	return "﻿".encode("utf-8") + ("\r\n".join(parts).rstrip() + "\r\n").encode("utf-8")


def renderMarkdown(title, blocks):
	parts = []
	if title:
		parts.append("# " + title)
		parts.append("")
	for kind, lines in _lines(blocks):
		if kind == "heading":
			parts.append("## " + " ".join(lines))
		else:
			parts.extend(lines)
		parts.append("")
	return ("\n".join(parts).rstrip() + "\n").encode("utf-8")


def renderHtml(title, blocks):
	body = []
	if title:
		body.append("<h1>%s</h1>" % html.escape(title))
	for kind, lines in _lines(blocks):
		if kind == "heading":
			body.append("<h2>%s</h2>" % html.escape(" ".join(lines)))
		else:
			for line in lines:
				body.append("<p>%s</p>" % html.escape(line) if line.strip() else "")
	page = (
		'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<title>%s</title>\n</head>\n<body>\n%s\n</body>\n</html>\n'
		% (html.escape(title or ""), "\n".join(line for line in body if line))
	)
	return page.encode("utf-8")


# Word (.docx): the smallest package Word opens: content types, relationships, document and styles.

_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def xmlText(text):
	"""Escape text for XML and drop characters XML 1.0 does not allow."""
	return html.escape(_XML_INVALID.sub("", text or ""), quote=False)


_CONTENT_TYPES = (
	'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
	'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
	'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
	'<Default Extension="xml" ContentType="application/xml"/>'
	'<Override PartName="/word/document.xml" '
	'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
	'<Override PartName="/word/styles.xml" '
	'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
	'<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
	"</Types>"
)
_ROOT_RELS = (
	'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
	'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
	'<Relationship Id="rId1" '
	'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
	'Target="word/document.xml"/>'
	'<Relationship Id="rId2" '
	'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
	'Target="docProps/core.xml"/>'
	"</Relationships>"
)
_DOCUMENT_RELS = (
	'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
	'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
	'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
	'Target="styles.xml"/>'
	"</Relationships>"
)


def _style(styleId, name, level=None, size=None):
	paragraphProperties = '<w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/><w:outlineLvl w:val="%d"/></w:pPr>' % level if level is not None else ""
	runProperties = '<w:rPr><w:b/><w:sz w:val="%d"/></w:rPr>' % size if size else ""
	return '<w:style w:type="paragraph" w:styleId="%s"><w:name w:val="%s"/>%s%s%s</w:style>' % (
		styleId,
		name,
		'<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>' if level is not None else '<w:qFormat/>',
		paragraphProperties,
		runProperties,
	)


_STYLES = (
	'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
	'<w:styles xmlns:w="%s">'
	'<w:docDefaults><w:rPrDefault><w:rPr><w:sz w:val="22"/></w:rPr></w:rPrDefault>'
	'<w:pPrDefault><w:pPr><w:spacing w:after="120"/></w:pPr></w:pPrDefault></w:docDefaults>'
	'<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
	"%s%s"
	"</w:styles>"
) % (_W, _style("Heading1", "heading 1", 0, 32), _style("Heading2", "heading 2", 1, 26))


def _paragraph(text, style=None):
	properties = '<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % style if style else ""
	if not text:
		return "<w:p>%s</w:p>" % properties
	return '<w:p>%s<w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % (properties, xmlText(text.replace("\t", "    ")))


def _coreProperties(title, now):
	stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
	return (
		'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
		'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
		'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
		'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
		"<dc:title>%s</dc:title><dc:creator>Ask AI</dc:creator>"
		'<dcterms:created xsi:type="dcterms:W3CDTF">%s</dcterms:created>'
		'<dcterms:modified xsi:type="dcterms:W3CDTF">%s</dcterms:modified>'
		"</cp:coreProperties>"
	) % (xmlText(title), stamp, stamp)


def renderDocx(title, blocks, now=None):
	now = now or datetime.datetime.now(datetime.timezone.utc)
	paragraphs = []
	if title:
		paragraphs.append(_paragraph(title, "Heading1"))
	for kind, lines in _lines(blocks):
		if kind == "heading":
			paragraphs.append(_paragraph(" ".join(lines), "Heading2"))
		else:
			paragraphs.extend(_paragraph(line) for line in lines)
	document = (
		'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
		'<w:document xmlns:w="%s"><w:body>%s'
		'<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
		'<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/>'
		"</w:sectPr></w:body></w:document>"
	) % (_W, "".join(paragraphs) or "<w:p/>")
	buffer = io.BytesIO()
	with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
		package.writestr("[Content_Types].xml", _CONTENT_TYPES)
		package.writestr("_rels/.rels", _ROOT_RELS)
		package.writestr("word/document.xml", document)
		package.writestr("word/_rels/document.xml.rels", _DOCUMENT_RELS)
		package.writestr("word/styles.xml", _STYLES)
		package.writestr("docProps/core.xml", _coreProperties(title or "", now))
	return buffer.getvalue()


# PDF: version 1.4, built-in Helvetica (no embedded fonts), WinAnsi encoding, A4 pages.
# Characters outside Windows-1252 cannot be shown with the built-in fonts and become "?".

# Helvetica advance widths (1/1000 em) for character codes 32 to 126, from the standard Adobe font metrics.
_HELVETICA_WIDTHS = (
	278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
	556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
	1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
	667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
	333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
	556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)
PAGE_WIDTH = 595
PAGE_HEIGHT = 842
MARGIN = 56
FONT_SIZE = 11
LEADING = 15
HEADING_SIZE = 14


def pdfEncode(text):
	"""Windows-1252 bytes for the built-in fonts; other characters become "?"."""
	return (text or "").replace("\t", "    ").encode("cp1252", errors="replace")


def textWidth(data, size):
	return sum(_HELVETICA_WIDTHS[b - 32] if 32 <= b <= 126 else 556 for b in data) * size / 1000.0


def wrapLine(data, size, maxWidth):
	"""Split encoded text into lines no wider than maxWidth, breaking at spaces, or inside very long words."""
	if not data.strip():
		return [b""]
	lines = []
	current = b""
	for word in data.split(b" "):
		candidate = word if not current else current + b" " + word
		if textWidth(candidate, size) <= maxWidth:
			current = candidate
			continue
		if current:
			lines.append(current)
		current = b""
		while textWidth(word, size) > maxWidth:
			cut = len(word)
			while cut > 1 and textWidth(word[:cut], size) > maxWidth:
				cut -= 1
			lines.append(word[:cut])
			word = word[cut:]
		current = word
	lines.append(current)
	return lines


def _pdfString(data):
	out = bytearray(b"(")
	for byte in data:
		if byte in (0x28, 0x29, 0x5C):  # ( ) \
			out += b"\\" + bytes((byte,))
		elif 32 <= byte <= 126:
			out.append(byte)
		else:
			out += b"\\%03o" % byte
	out += b")"
	return bytes(out)


def _pdfTextString(text):
	"""A PDF text string for metadata: UTF-16BE with a byte order mark, as hex."""
	return b"<FEFF" + (text or "").encode("utf-16-be").hex().upper().encode("ascii") + b">"


def _layoutPdf(title, blocks):
	"""List of pages; each page is a list of (fontSize, encodedLine)."""
	usableWidth = PAGE_WIDTH - 2 * MARGIN
	rowsPerPage = int((PAGE_HEIGHT - 2 * MARGIN) // LEADING)
	rows = []
	if title:
		rows.extend((HEADING_SIZE, line) for line in wrapLine(pdfEncode(title), HEADING_SIZE, usableWidth))
		rows.append((FONT_SIZE, b""))
	for kind, lines in _lines(blocks):
		size = HEADING_SIZE if kind == "heading" else FONT_SIZE
		for line in lines:
			rows.extend((size, part) for part in wrapLine(pdfEncode(line), size, usableWidth))
		rows.append((FONT_SIZE, b""))
	while rows and not rows[-1][1]:
		rows.pop()
	pages = [rows[i:i + rowsPerPage] for i in range(0, len(rows), rowsPerPage)]
	return pages or [[]]


def renderPdf(title, blocks):
	pages = _layoutPdf(title, blocks)
	objects = {}
	fontRegular, fontBold = 3, 4
	objects[fontRegular] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
	objects[fontBold] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
	pageIds = []
	nextId = 5
	for page in pages:
		stream = bytearray(b"BT\n%d TL\n%d %d Td\n" % (LEADING, MARGIN, PAGE_HEIGHT - MARGIN - FONT_SIZE))
		currentFont = None
		for size, line in page:
			font = (b"F2", size) if size == HEADING_SIZE else (b"F1", size)
			if font != currentFont:
				stream += b"/%s %d Tf\n" % font
				currentFont = font
			stream += _pdfString(line) + b" Tj T*\n"
		stream += b"ET\n"
		contentId, pageId = nextId, nextId + 1
		nextId += 2
		objects[contentId] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), bytes(stream))
		objects[pageId] = (
			b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R "
			b"/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> >>"
			% (PAGE_WIDTH, PAGE_HEIGHT, contentId, fontRegular, fontBold)
		)
		pageIds.append(pageId)
	objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
	objects[2] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
		b" ".join(b"%d 0 R" % pageId for pageId in pageIds),
		len(pageIds),
	)
	infoId = nextId
	objects[infoId] = b"<< /Title %s /Producer (Ask AI for NVDA) >>" % _pdfTextString(title or "")
	output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
	offsets = {}
	for objectId in sorted(objects):
		offsets[objectId] = len(output)
		output += b"%d 0 obj\n%s\nendobj\n" % (objectId, objects[objectId])
	xrefOffset = len(output)
	count = max(objects) + 1
	output += b"xref\n0 %d\n0000000000 65535 f \n" % count
	for objectId in range(1, count):
		output += b"%010d 00000 n \n" % offsets[objectId]
	output += b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (count, infoId, xrefOffset)
	return bytes(output)


RENDERERS = {
	FORMAT_DOCX: renderDocx,
	FORMAT_PDF: renderPdf,
	FORMAT_TXT: renderText,
	FORMAT_HTML: renderHtml,
	FORMAT_MD: renderMarkdown,
}


def render(formatKey, title, blocks):
	return RENDERERS[formatKey](title, blocks)


def writeNew(folder, baseName, extension, data):
	"""Write data to a new file, never replacing an existing one. Returns the path."""
	os.makedirs(folder, exist_ok=True)
	baseName = sanitizeFileName(baseName)
	for _attempt in range(1000):
		path = uniquePath(folder, baseName, extension)
		try:
			with open(path, "xb") as f:  # "x" fails if the file appeared meanwhile.
				f.write(data)
			return path
		except FileExistsError:
			continue
	raise OSError("Could not find a free file name in %s" % folder)


def export(folder, baseName, formatKey, title, blocks, markdownBlocks=None):
	"""Render and save. Markdown uses markdownBlocks (the raw text) when given. Returns the path."""
	data = render(formatKey, title, markdownBlocks if formatKey == FORMAT_MD and markdownBlocks is not None else blocks)
	return writeNew(folder, baseName, EXTENSIONS[formatKey], data)
