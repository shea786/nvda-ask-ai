# -*- coding: UTF-8 -*-
# Ask GPT NVDA add-on: keep NVDA quiet while Windows voice typing types into the Question box,
# then restore the user's exact speech mode. No NVDA imports: the speech module is passed in,
# so the state machine can be tested with a fake.
#
# NVDA API (identical in 2024.1, 2025.1 and 2026 master, source/speech/speech.py):
#   speech.getState().speechMode -> speech.SpeechMode member (off, beeps, talk, onDemand)
#   speech.setSpeechMode(mode)
# NVDA's own "speech mode" command (globalCommands.script_speechMode) uses the same two calls.

import time

DEFAULT_SILENCE_TIMEOUT = 4.0
MIN_SILENCE_TIMEOUT = 2
MAX_SILENCE_TIMEOUT = 15
NOTHING_TYPED_TIMEOUT = 20.0
# Opening voice typing may briefly move focus; do not treat that as the user leaving.
FOCUS_GRACE_SECONDS = 1.5

# Why dictation ended.
REASON_SEND = "send"  # Enter: restore quietly, then send the question.
REASON_ESCAPE = "escape"
REASON_TOGGLE = "toggle"  # Dictate pressed again.
REASON_FOCUS = "focusLost"
REASON_IDLE = "idle"  # Text changed, then stayed the same for the silence timeout.
REASON_NOTHING = "nothingTyped"
REASON_CLOSE = "close"  # Dialog closed, NVDA exiting, or an error: restore quietly.


class SpeechModeAdapter:
	"""Reads and sets NVDA's speech mode through the speech module, tolerating missing APIs."""

	def __init__(self, speechModule):
		self._speech = speechModule

	@property
	def available(self):
		speech = self._speech
		return (
			hasattr(speech, "getState")
			and hasattr(speech, "setSpeechMode")
			and hasattr(getattr(speech, "SpeechMode", None), "off")
		)

	def get(self):
		return self._speech.getState().speechMode

	def set(self, mode):
		self._speech.setSpeechMode(mode)

	@property
	def off(self):
		return self._speech.SpeechMode.off


def clampSilenceTimeout(value):
	try:
		value = float(value)
	except (TypeError, ValueError):
		return DEFAULT_SILENCE_TIMEOUT
	return max(MIN_SILENCE_TIMEOUT, min(MAX_SILENCE_TIMEOUT, value))


class DictationSession:
	"""One dictation: mute on start(), restore exactly once on finish(), whatever triggers it."""

	def __init__(self, speechMode, clock=time.monotonic, silenceTimeout=DEFAULT_SILENCE_TIMEOUT):
		self._speechMode = speechMode
		self._clock = clock
		self.silenceTimeout = clampSilenceTimeout(silenceTimeout)
		self._savedMode = None
		self.active = False
		self.finishedReason = None
		self.initialText = ""
		self._lastText = ""
		self._started = 0.0
		self._lastChange = None

	def start(self, initialText):
		"""Save the current speech mode and switch speech off. Returns False (and changes nothing)
		if NVDA's speech mode API is not available.
		"""
		if self.active:
			return True
		if not self._speechMode.available:
			return False
		self._savedMode = self._speechMode.get()
		self.initialText = initialText or ""
		self._lastText = self.initialText
		self._started = self._clock()
		self._lastChange = None
		self.finishedReason = None
		self.active = True
		try:
			self._speechMode.set(self._speechMode.off)
		except Exception:
			self.active = False
			self._savedMode = None
			raise
		return True

	@property
	def savedMode(self):
		return self._savedMode

	def poll(self, text, focused=True):
		"""Check the end conditions (call every ~400 ms). Returns a REASON_* or None."""
		if not self.active:
			return None
		now = self._clock()
		text = text or ""
		if text != self._lastText:
			self._lastText = text
			self._lastChange = now
		if not focused and now - self._started >= FOCUS_GRACE_SECONDS:
			return REASON_FOCUS
		if self._lastChange is not None:
			if now - self._lastChange >= self.silenceTimeout:
				return REASON_IDLE
		elif now - self._started >= NOTHING_TYPED_TIMEOUT:
			return REASON_NOTHING
		return None

	def dictatedText(self, text):
		"""What voice typing added: the new text after what was there before, or the whole text."""
		text = (text or "").strip()
		initial = self.initialText.strip()
		if initial and text.startswith(initial):
			return text[len(initial):].strip()
		return "" if text == initial else text

	def finish(self, reason):
		"""Restore the saved speech mode. Only the first call does anything; returns True for it."""
		if not self.active:
			return False
		self.active = False
		self.finishedReason = reason
		mode, self._savedMode = self._savedMode, None
		self._speechMode.set(mode)
		return True
