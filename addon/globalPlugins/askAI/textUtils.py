# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: pure text helpers (no NVDA imports, testable anywhere).

import re

_FENCE_LINE = re.compile(r"^[ \t]*(```+|~~~+)")
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
# A run of N backticks closes only on another run of exactly N, so content may contain shorter runs.
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)([^\n]+?)(?<!`)\1(?!`)")
# A table separator row has at least one pipe, so a plain "---" rule is not mistaken for one.
_TABLE_SEPARATOR = re.compile(
	r"^(?=[^\n]*\|)[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*(\n|$)",
	re.MULTILINE,
)
_TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.MULTILINE)
_MANY_BLANK_LINES = re.compile(r"\n{3,}")
# Placeholders for protected inline code; \x00 never occurs in model text.
_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")


def _tableRow(match):
	cells = [cell.strip() for cell in match.group(0).strip().strip("|").split("|")]
	return ", ".join(cell for cell in cells if cell)


def _stripProse(text):
	"""Strip Markdown from text that is not inside a code fence. Inline code content is kept verbatim."""
	codeSpans = []

	def protect(match):
		codeSpans.append(match.group(2))
		return "\x00%d\x00" % (len(codeSpans) - 1)

	text = _INLINE_CODE.sub(protect, text)
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
	return _PLACEHOLDER.sub(lambda match: codeSpans[int(match.group(1))], text)


def _isClosingFence(line, fence):
	"""A closing fence uses the same character, is at least as long, and has nothing after it."""
	marker = line.strip()
	return bool(marker) and set(marker) == {fence[0]} and len(marker) >= len(fence)


def stripMarkdown(text):
	"""Remove simple Markdown so that a screen reader does not read out symbols.
	Handles headings, bold, italics, strikethrough, links, images, block quotes, bullets,
	horizontal rules and simple tables. Code is never altered: the fence lines of a code block
	are removed but its content is kept exactly, and inline code keeps its content (only the
	backticks go).
	"""
	if not text:
		return ""
	text = text.replace("\r\n", "\n").replace("\r", "\n")
	output = []
	prose = []
	fence = None
	for line in text.split("\n"):
		match = _FENCE_LINE.match(line)
		if fence is None:
			if match:
				output.append(_stripProse("\n".join(prose)) if prose else None)
				prose = []
				fence = match.group(1)
			else:
				prose.append(line)
		elif _isClosingFence(line, fence):
			fence = None
		else:
			output.append(line)
	if prose:
		output.append(_stripProse("\n".join(prose)))
	text = "\n".join(part for part in output if part is not None)
	text = _MANY_BLANK_LINES.sub("\n\n", text)
	return text.strip()


def formatTurn(speaker, text):
	"""Format one conversation turn for the read-only conversation view."""
	return "%s: %s" % (speaker, (text or "").strip())


def estimatedSpeechMs(text, msPerChar=75, minimum=1500, maximum=12000):
	"""Rough time NVDA needs to speak text at a typical rate, used as a fallback delay."""
	return max(minimum, min(maximum, 500 + msPerChar * len(text or "")))
