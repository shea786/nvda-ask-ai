# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: the text of "Report Ask AI status" and of the finished/failed announcements
# made while the user works in another app (phrases mirror T3 Code's announcer).
# Pure standard library, no NVDA imports; the UI passes translated templates.

import datetime
import time

DEFAULT_TEMPLATES = {
	"answering": "{service} is answering in {title}, {elapsed}.",
	"dictating": "Dictating.",
	"signingIn": "Waiting for sign-in.",
	"notSignedIn": "Not signed in to {service}",
	"idleWithAnswer": "Idle. Last answer from {service} at {time} in {title}.",
	"idle": "Idle.",
	"seconds": "{seconds} seconds",
	"oneSecond": "1 second",
	"minutes": "{minutes} minutes {seconds} seconds",
	"done": "Done: {title}.",
	"failed": "Failed: {title}. {reason}",
	"untitled": "Untitled conversation",
}


class StatusSnapshot:
	"""What the add-on is doing right now. All fields are optional.
	@ivar answering: (service, title, startedAt) of a running request, or None.
	@ivar dictating: True while voice typing is muting NVDA.
	@ivar signingIn: True while a sign-in waits for the browser.
	@ivar service: the service of the current conversation, e.g. "Claude".
	@ivar signedOut: True when the last known sign-in status of that service is "signed out".
	@ivar lastAnswer: (service, answeredAt, title) of the newest answer, or None.
	"""

	def __init__(self, answering=None, dictating=False, signingIn=False, service="", signedOut=False, lastAnswer=None):
		self.answering = answering
		self.dictating = dictating
		self.signingIn = signingIn
		self.service = service
		self.signedOut = signedOut
		self.lastAnswer = lastAnswer


def formatElapsed(seconds, templates=DEFAULT_TEMPLATES):
	seconds = max(0, int(seconds))
	if seconds == 1:
		return templates["oneSecond"]
	if seconds < 60:
		return templates["seconds"].format(seconds=seconds)
	return templates["minutes"].format(minutes=seconds // 60, seconds=seconds % 60)


def formatClock(timestamp, now=None):
	"""12-hour clock, "3:05 PM"; with the date ("2026-10-30 3:05 PM") when it is not today."""
	moment = datetime.datetime.fromtimestamp(timestamp)
	today = datetime.datetime.fromtimestamp(time.time() if now is None else now).date()
	hour = moment.hour % 12 or 12
	clock = "%d:%02d %s" % (hour, moment.minute, "AM" if moment.hour < 12 else "PM")
	if moment.date() == today:
		return clock
	return "%s %s" % (moment.strftime("%Y-%m-%d"), clock)


def _title(title, templates):
	return (title or "").strip() or templates["untitled"]


def statusText(snapshot, now=None, templates=DEFAULT_TEMPLATES):
	"""One sentence: answering, else dictating, else waiting for sign-in, else signed out, else idle."""
	now = time.time() if now is None else now
	if snapshot.answering:
		service, title, startedAt = snapshot.answering
		return templates["answering"].format(
			service=service,
			title=_title(title, templates),
			elapsed=formatElapsed(now - startedAt, templates),
		)
	if snapshot.dictating:
		return templates["dictating"]
	if snapshot.signingIn:
		return templates["signingIn"]
	if snapshot.signedOut and snapshot.service:
		return templates["notSignedIn"].format(service=snapshot.service)
	if snapshot.lastAnswer:
		service, answeredAt, title = snapshot.lastAnswer
		return templates["idleWithAnswer"].format(
			service=service,
			time=formatClock(answeredAt, now),
			title=_title(title, templates),
		)
	return templates["idle"]


def doneText(title, answer, templates=DEFAULT_TEMPLATES):
	"""The answer prefixed with "Done: <title>." (answer may be empty)."""
	prefix = templates["done"].format(title=_title(title, templates))
	return ("%s %s" % (prefix, answer)).strip() if answer else prefix


def failedText(title, reason, templates=DEFAULT_TEMPLATES):
	return templates["failed"].format(title=_title(title, templates), reason=reason or "").strip()
