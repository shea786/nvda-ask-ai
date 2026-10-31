# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: "Ask about this" - text from the user's screen attached to the next question.
# The NVDA glue passes in the focus and navigator objects and functions that read their text
# (TextInfo POSITION_SELECTION / POSITION_ALL), so this module stays free of NVDA imports.

import re

MAX_CONTEXT_CHARS = 20000

SUMMARY_TEMPLATES = {
	"selection": "Selected text from {app}, {count} characters",
	"selectionCut": "Selected text from {app}, cut to {count} characters",
	"object": "Text of the current object ({role}) from {app}, {count} characters",
	"objectCut": "Text of the current object ({role}) from {app}, cut to {count} characters",
	"objectNoRole": "Text of the current object from {app}, {count} characters",
	"objectNoRoleCut": "Text of the current object from {app}, cut to {count} characters",
	"theScreen": "the screen",
}

REASON_PROTECTED = "protected"
REASON_EMPTY = "empty"


class ContextError(Exception):
	"""Context could not be captured. reason is REASON_PROTECTED or REASON_EMPTY."""

	def __init__(self, reason):
		super().__init__(reason)
		self.reason = reason


def isProtectedObject(obj, passwordRole=None, protectedState=None):
	"""True for password and other protected fields (NVDAObject.isProtected, the password-edit role,
	or the protected state). Any error counts as protected, to be safe.
	"""
	if obj is None:
		return False
	try:
		if getattr(obj, "isProtected", False):
			return True
		if passwordRole is not None and getattr(obj, "role", None) == passwordRole:
			return True
		states = getattr(obj, "states", None) or ()
		return protectedState is not None and protectedState in states
	except Exception:
		return True


def _clean(text, limit=150):
	text = re.sub(r"[`\r\n\t\x00-\x1f]+", " ", text or "")
	return re.sub(r" {2,}", " ", text).strip()[:limit]


def _safe(read, obj):
	if obj is None or read is None:
		return ""
	try:
		value = read(obj)
	except Exception:
		return ""
	return value if isinstance(value, str) else ""


class ScreenContext:
	"""Text from the screen for the next question.
	@ivar text: the captured text (at most MAX_CONTEXT_CHARS characters).
	@ivar source: "selection" or "navigator".
	@ivar appName: the application, e.g. "Microsoft Word" or "winword".
	@ivar windowTitle: the foreground window's title.
	@ivar truncated: True when the text was cut to MAX_CONTEXT_CHARS.
	@ivar screenshot: (width, height, BGRA bytes) of the window, captured when the question is sent and
	only if the user ticked "Include a screenshot", or None.
	@ivar hwnd: the foreground window when the text was captured (for a screenshot at send time), or 0.
	@ivar roleName: the navigator object's role ("document", "button") for the summary, or "".
	"""

	def __init__(self, text, source, appName="", windowTitle="", truncated=False, screenshot=None, hwnd=0, roleName=""):
		self.text = text
		self.source = source
		self.appName = appName or ""
		self.windowTitle = windowTitle or ""
		self.truncated = truncated
		self.screenshot = screenshot
		self.hwnd = hwnd or 0
		self.roleName = roleName or ""

	def summary(self, templates=SUMMARY_TEMPLATES):
		"""Source, application and size, never the text: "Selected text from Word, 1,240 characters" or
		"Text of the current object (document) from Edge, cut to 20,000 characters".
		"""
		app = _clean(self.appName) or _clean(self.windowTitle) or templates["theScreen"]
		if self.source == "selection":
			key = "selection"
		elif self.roleName:
			key = "object"
		else:
			key = "objectNoRole"
		if self.truncated:
			key += "Cut"
		return templates[key].format(app=app, role=_clean(self.roleName), count="{:,}".format(len(self.text)))

	@property
	def windowName(self):
		"""The window for the screenshot checkbox label."""
		return _clean(self.windowTitle) or _clean(self.appName)

	def label(self):
		"""Label of the fenced prompt block (application and window, never the text). The names come from
		other programs, so backticks and line breaks are removed (they could otherwise end the fence).
		"""
		where = ", ".join(_clean(part) for part in (self.appName, self.windowTitle) if _clean(part))
		return "Text from the user's screen%s" % ((" (%s)" % where) if where else "")


def captureContext(
	focus,
	navigator,
	getSelection,
	getAllText,
	appName="",
	windowTitle="",
	isProtected=isProtectedObject,
	maxChars=MAX_CONTEXT_CHARS,
	roleName="",
	hwnd=0,
):
	"""The selected text of the focus object; otherwise the navigator object's name, value and text.
	@raise ContextError: REASON_PROTECTED for protected (password) fields, REASON_EMPTY when there is no text.
	"""
	if isProtected(focus) or isProtected(navigator):
		raise ContextError(REASON_PROTECTED)
	text = _safe(getSelection, focus)
	source = "selection"
	if not text.strip():
		source = "navigator"
		values = []
		for value in (
			_safe(lambda obj: obj.name, navigator),
			_safe(lambda obj: obj.value, navigator),
			_safe(getAllText, navigator),
		):
			value = (value or "").strip()
			if value and value not in values:
				values.append(value)
		# A part already contained in a longer one (a name repeated in the text) is left out.
		parts = [value for value in values if not any(value != other and value in other for other in values)]
		text = "\n".join(parts)
	text = text.strip("\r\n")
	if not text.strip():
		raise ContextError(REASON_EMPTY)
	truncated = len(text) > maxChars
	if truncated:
		text = text[:maxChars]
	return ScreenContext(text, source, appName, windowTitle, truncated, hwnd=hwnd, roleName=roleName if source != "selection" else "")
