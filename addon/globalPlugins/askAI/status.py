# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: the text of "Report Ask AI status" and of the finished/failed announcements
# made while the user works in another app.
# Pure standard library, no NVDA imports; the UI passes translated templates and a locale time formatter.

import datetime
import time

DEFAULT_TEMPLATES = {
	"answering": "{service}, working for {elapsed}. Conversation: {title}.",
	"answeringJustStarted": "{service}, just started. Conversation: {title}.",
	"dictating": "Dictating.",
	"signingIn": "Waiting for sign-in.",
	"notSignedIn": "Not signed in to {service}.",
	"idleWithAnswer": "Idle. Last answer from {service} at {time} in {title}.",
	"idle": "Idle.",
	"seconds": "{seconds} seconds",
	"oneSecond": "1 second",
	"minutes": "{minutes} minutes {seconds} seconds",
	"done": "Ask AI answer, {title}:",
	"failed": "Failed: {title}. {reason}",
	"failedQuestion": "Failed: {title} {reason}",
	"untitled": "Untitled conversation",
	"oneFile": "1 file",
	"files": "{count} files",
	"attachedBoth": "Context from {app} and {files} attached to the next question.",
	"attachedContext": "Context from {app} attached to the next question.",
	"attachedFiles": "{files} attached to the next question.",
}


class StatusSnapshot:
	"""What the add-on is doing right now. All fields are optional.
	@ivar answering: (service, title, startedAt) of a running request, or None.
	@ivar dictating: True while voice typing is muting NVDA.
	@ivar signingIn: True while a sign-in waits for the browser.
	@ivar service: the service of the current conversation, e.g. "Claude".
	@ivar signedOut: True when the last known sign-in status of that service is "signed out".
	@ivar lastAnswer: (service, answeredAt, title) of the newest answer, or None.
	@ivar contextApp: the application of screen text attached to the next question, or "".
	@ivar attachedFiles: how many files are attached to the next question.
	"""

	def __init__(
		self,
		answering=None,
		dictating=False,
		signingIn=False,
		service="",
		signedOut=False,
		lastAnswer=None,
		contextApp="",
		attachedFiles=0,
	):
		self.answering = answering
		self.dictating = dictating
		self.signingIn = signingIn
		self.service = service
		self.signedOut = signedOut
		self.lastAnswer = lastAnswer
		self.contextApp = contextApp
		self.attachedFiles = attachedFiles


def formatElapsed(seconds, templates=DEFAULT_TEMPLATES):
	seconds = max(0, int(seconds))
	if seconds == 1:
		return templates["oneSecond"]
	if seconds < 60:
		return templates["seconds"].format(seconds=seconds)
	return templates["minutes"].format(minutes=seconds // 60, seconds=seconds % 60)


def formatClock(timestamp, now=None):
	"""Fallback clock without locale: 12-hour "3:05 PM", with the date when it is not today.
	The NVDA UI passes a formatter that uses the Windows locale instead.
	"""
	moment = datetime.datetime.fromtimestamp(timestamp)
	today = datetime.datetime.fromtimestamp(time.time() if now is None else now).date()
	hour = moment.hour % 12 or 12
	clock = "%d:%02d %s" % (hour, moment.minute, "AM" if moment.hour < 12 else "PM")
	if moment.date() == today:
		return clock
	return "%s %s" % (moment.strftime("%Y-%m-%d"), clock)


def _title(title, templates):
	return (title or "").strip() or templates["untitled"]


def _sentenceTitle(title, templates):
	"""The title for "... {title}. ...": a final full stop, question or exclamation mark is not doubled."""
	return _title(title, templates).rstrip(".")


def _formatWithTitle(template, title, **values):
	"""Fill a template whose {title} ends a sentence; a title ending in "?" or "!" gets no extra full stop."""
	text = template.format(title=title, **values)
	if title.endswith(("?", "!")):
		text = text.replace(title + ".", title, 1)
	return text


def attachedText(snapshot, templates=DEFAULT_TEMPLATES):
	"""What goes with the next question, or ""."""
	count = snapshot.attachedFiles
	files = templates["oneFile"] if count == 1 else templates["files"].format(count=count)
	if snapshot.contextApp and count:
		return templates["attachedBoth"].format(app=snapshot.contextApp, files=files)
	if snapshot.contextApp:
		return templates["attachedContext"].format(app=snapshot.contextApp)
	if count:
		return templates["attachedFiles"].format(files=files)
	return ""


def statusText(snapshot, now=None, templates=DEFAULT_TEMPLATES, clock=None):
	"""Answering, else dictating, else waiting for sign-in, else signed out, else idle; then anything attached."""
	now = time.time() if now is None else now
	clock = clock or (lambda timestamp: formatClock(timestamp, now))
	if snapshot.answering:
		service, title, startedAt = snapshot.answering
		elapsed = now - startedAt
		if elapsed < 1:
			text = _formatWithTitle(templates["answeringJustStarted"], _sentenceTitle(title, templates), service=service)
		else:
			text = _formatWithTitle(
				templates["answering"],
				_sentenceTitle(title, templates),
				service=service,
				elapsed=formatElapsed(elapsed, templates),
			)
	elif snapshot.dictating:
		text = templates["dictating"]
	elif snapshot.signingIn:
		text = templates["signingIn"]
	elif snapshot.signedOut and snapshot.service:
		text = templates["notSignedIn"].format(service=snapshot.service)
	elif snapshot.lastAnswer:
		service, answeredAt, title = snapshot.lastAnswer
		text = _formatWithTitle(
			templates["idleWithAnswer"],
			_sentenceTitle(title, templates),
			service=service,
			time=clock(answeredAt),
		)
	else:
		text = templates["idle"]
	attached = attachedText(snapshot, templates)
	return "%s %s" % (text, attached) if attached else text


def doneText(title, answer, templates=DEFAULT_TEMPLATES):
	""""Ask AI answer, <title>:" followed by the answer (which may be empty)."""
	prefix = templates["done"].format(title=_title(title, templates).rstrip(".:"))
	return ("%s %s" % (prefix, answer)).strip() if answer else prefix


def failedText(title, reason, templates=DEFAULT_TEMPLATES):
	""""Failed: <title>. <reason>"; a title ending in "?" or "!" gets no extra full stop."""
	title = _title(title, templates)
	key = "failedQuestion" if title.endswith(("?", "!")) else "failed"
	return templates[key].format(title=title.rstrip("."), reason=reason or "").strip()
