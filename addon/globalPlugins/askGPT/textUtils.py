# -*- coding: UTF-8 -*-
# Ask GPT NVDA add-on: pure text helpers (no NVDA imports, testable anywhere).

import re

_CODE_FENCE = re.compile(r"^[ \t]*(```|~~~)[^\n]*(\n|$)", re.MULTILINE)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.MULTILINE)
_HORIZONTAL_RULE = re.compile(r"^[ \t]*([-*_])([ \t]*\1){2,}[ \t]*$", re.MULTILINE)
_BULLET = re.compile(r"^([ \t]*)[*+\-][ \t]+", re.MULTILINE)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:[ \t]+\"[^\"]*\")?\)")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_ITALIC_STAR = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_ITALIC_UNDERSCORE = re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_INLINE_CODE = re.compile(r"`+([^`\n]*?)`+")
# A table separator row has at least one pipe, so a plain "---" rule is not mistaken for one.
_TABLE_SEPARATOR = re.compile(
	r"^(?=[^\n]*\|)[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*(\n|$)",
	re.MULTILINE,
)
_MANY_BLANK_LINES = re.compile(r"\n{3,}")


def _tableRow(match):
	cells = [cell.strip() for cell in match.group(0).strip().strip("|").split("|")]
	return ", ".join(cell for cell in cells if cell)


_TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.MULTILINE)


def stripMarkdown(text):
	"""Remove simple Markdown so that a screen reader does not read out symbols.
	Handles headings, bold, italics, strikethrough, inline code and code fences,
	links, images, block quotes, bullets, horizontal rules and simple tables.
	The text content itself is preserved.
	"""
	if not text:
		return ""
	text = text.replace("\r\n", "\n").replace("\r", "\n")
	text = _CODE_FENCE.sub("", text)
	text = _TABLE_SEPARATOR.sub("", text)
	text = _TABLE_ROW.sub(_tableRow, text)
	text = _HORIZONTAL_RULE.sub("", text)
	text = _HEADING.sub(r"\1", text)
	text = _BLOCKQUOTE.sub("", text)
	text = _BULLET.sub(r"\1", text)
	text = _IMAGE.sub(r"\1", text)
	text = _LINK.sub(r"\1 (\2)", text)
	text = _BOLD.sub(r"\2", text)
	text = _STRIKE.sub(r"\1", text)
	text = _ITALIC_STAR.sub(r"\1", text)
	text = _ITALIC_UNDERSCORE.sub(r"\1", text)
	text = _INLINE_CODE.sub(r"\1", text)
	text = _MANY_BLANK_LINES.sub("\n\n", text)
	return text.strip()


def formatTurn(speaker, text):
	"""Format one conversation turn for the read-only conversation view."""
	return "%s: %s" % (speaker, (text or "").strip())
