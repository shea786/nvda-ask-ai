# -*- coding: UTF-8 -*-
# QA only (smoke userConfig, never the Ask AI add-on): a synthesizer that says nothing and records every text
# NVDA hands to it, with a time stamp, in `transcript` and as "ASKAI-SYNTH: <text>" lines in nvda.log.
# Modelled on NVDA 2025.3's synthDrivers/silence.py, plus index and done-speaking notifications so NVDA's
# speech manager (and CallbackCommand, used by Ask AI's speakThen) behaves as with a real synth.
# Selected with [speech] synth = askaiRecorder (run.sh does this for ASKAI_SMOKE_ONLY=dictation).

import time
from collections import OrderedDict

import wx

import synthDriverHandler
from logHandler import log
from speech.commands import IndexCommand

transcript = []  # (time.monotonic(), text), one entry per speak() call with text.


class SynthDriver(synthDriverHandler.SynthDriver):
	name = "askaiRecorder"
	description = "Ask AI smoke recorder (QA, no sound)"
	supportedSettings = frozenset()
	supportedCommands = {IndexCommand}
	supportedNotifications = {synthDriverHandler.synthIndexReached, synthDriverHandler.synthDoneSpeaking}
	_availableVoices = OrderedDict({name: synthDriverHandler.VoiceInfo(name, description)})

	@classmethod
	def check(cls):
		return True

	def speak(self, speechSequence):
		text = " ".join(item for item in speechSequence if isinstance(item, str)).strip()
		if text:
			transcript.append((time.monotonic(), text))
			log.info("ASKAI-SYNTH: %s" % text)
		indexes = [item.index for item in speechSequence if isinstance(item, IndexCommand)]
		# Report indexes and the end of speech later, as a real synth does from its audio thread.
		wx.CallAfter(self._finished, indexes)

	def _finished(self, indexes):
		for index in indexes:
			synthDriverHandler.synthIndexReached.notify(synth=self, index=index)
		synthDriverHandler.synthDoneSpeaking.notify(synth=self)

	def cancel(self):
		pass

	def _get_voice(self):
		return self.name
