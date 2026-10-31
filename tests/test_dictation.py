"""Feature 1: NVDA is silent while Windows voice typing types, and speech always comes back exactly as it was."""

import types
import unittest

import helpers  # noqa: F401  (adds the add-on package to sys.path)

import dictation


class FakeSpeech:
	"""Stand-in for NVDA's speech module: getState().speechMode, setSpeechMode(), SpeechMode."""

	class SpeechMode:
		off = "off"
		beeps = "beeps"
		talk = "talk"
		onDemand = "onDemand"

	def __init__(self, mode="talk"):
		self.mode = mode
		self.calls = []

	def getState(self):
		return types.SimpleNamespace(speechMode=self.mode)

	def setSpeechMode(self, mode):
		self.calls.append(mode)
		self.mode = mode


class FakeClock:
	def __init__(self):
		self.now = 100.0

	def __call__(self):
		return self.now

	def advance(self, seconds):
		self.now += seconds


class TestDictationSession(unittest.TestCase):
	def make(self, mode="talk", silence=4):
		self.speech = FakeSpeech(mode)
		self.clock = FakeClock()
		return dictation.DictationSession(dictation.SpeechModeAdapter(self.speech), clock=self.clock, silenceTimeout=silence)

	def test_startMutesAndFinishRestoresSavedMode(self):
		for mode in ("talk", "beeps", "onDemand", "off"):
			session = self.make(mode)
			self.assertTrue(session.start(""))
			self.assertEqual(self.speech.mode, "off")
			self.assertEqual(session.savedMode, mode)
			self.assertTrue(session.finish(dictation.REASON_IDLE))
			self.assertEqual(self.speech.mode, mode, "restores the saved mode, not just talk")

	def test_doubleFinishIsANoOp(self):
		session = self.make("onDemand")
		session.start("")
		self.assertTrue(session.finish(dictation.REASON_SEND))
		self.speech.setSpeechMode("talk")  # The user changes mode afterwards.
		self.assertFalse(session.finish(dictation.REASON_CLOSE))
		self.assertEqual(self.speech.mode, "talk", "a second restore must not overwrite the user's new mode")
		self.assertEqual(session.finishedReason, dictation.REASON_SEND)

	def test_finishWithoutStartDoesNothing(self):
		session = self.make()
		self.assertFalse(session.finish(dictation.REASON_CLOSE))
		self.assertEqual(self.speech.calls, [])

	def test_idleAfterTextChanged(self):
		session = self.make(silence=4)
		session.start("Hello")
		self.clock.advance(1)
		self.assertIsNone(session.poll("Hello what"))
		self.clock.advance(3.9)
		self.assertIsNone(session.poll("Hello what"))
		self.clock.advance(0.2)
		self.assertIsNone(session.poll("Hello what is"), "a new change restarts the silence timer")
		self.clock.advance(4)
		self.assertEqual(session.poll("Hello what is"), dictation.REASON_IDLE)

	def test_nothingTypedTimeout(self):
		session = self.make(silence=2)
		session.start("")
		self.clock.advance(19.9)
		self.assertIsNone(session.poll(""))
		self.clock.advance(0.2)
		self.assertEqual(session.poll(""), dictation.REASON_NOTHING)

	def test_focusLossAfterGracePeriod(self):
		session = self.make()
		session.start("")
		self.clock.advance(0.5)
		self.assertIsNone(session.poll("", focused=False), "opening voice typing may move focus briefly")
		self.clock.advance(1.5)
		self.assertEqual(session.poll("", focused=False), dictation.REASON_FOCUS)

	def test_pollAfterFinishIsNone(self):
		session = self.make()
		session.start("")
		session.finish(dictation.REASON_TOGGLE)
		self.clock.advance(60)
		self.assertIsNone(session.poll("x", focused=False))

	def test_dictatedText(self):
		session = self.make()
		session.start("Tell me ")
		self.assertEqual(session.dictatedText("Tell me about Paris."), "about Paris.")
		self.assertEqual(session.dictatedText("Tell me"), "")
		self.assertEqual(session.dictatedText("Something else"), "Something else")
		empty = self.make()
		empty.start("")
		self.assertEqual(empty.dictatedText("  Hello.  "), "Hello.")

	def test_silenceTimeoutIsClamped(self):
		self.assertEqual(dictation.clampSilenceTimeout(1), 2)
		self.assertEqual(dictation.clampSilenceTimeout(30), 15)
		self.assertEqual(dictation.clampSilenceTimeout("bad"), dictation.DEFAULT_SILENCE_TIMEOUT)
		self.assertEqual(self.make(silence=99).silenceTimeout, 15)

	def test_missingSpeechApiMeansNoMuting(self):
		old = types.SimpleNamespace()  # No getState/setSpeechMode at all.
		session = dictation.DictationSession(dictation.SpeechModeAdapter(old))
		self.assertFalse(session.start(""))
		self.assertFalse(session.active)
		self.assertFalse(session.finish(dictation.REASON_CLOSE))

	def test_failedMuteLeavesNothingActive(self):
		speech = FakeSpeech()

		def broken(mode):
			raise RuntimeError("boom")

		speech.setSpeechMode = broken
		session = dictation.DictationSession(dictation.SpeechModeAdapter(speech))
		with self.assertRaises(RuntimeError):
			session.start("")
		self.assertFalse(session.active)

	def test_everyReasonRestores(self):
		for reason in (
			dictation.REASON_SEND,
			dictation.REASON_ESCAPE,
			dictation.REASON_TOGGLE,
			dictation.REASON_FOCUS,
			dictation.REASON_IDLE,
			dictation.REASON_NOTHING,
			dictation.REASON_CLOSE,
		):
			session = self.make("beeps")
			session.start("")
			session.finish(reason)
			self.assertEqual(self.speech.mode, "beeps", reason)


if __name__ == "__main__":
	unittest.main()
