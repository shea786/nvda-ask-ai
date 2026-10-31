# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: NVDA side of the update check (timer, the update dialog, handing the file to NVDA's
# installer). The network and verification logic is in updater.py.

import os
import threading
import time

import wx

import addonHandler
import globalVars
import ui
from logHandler import log

from . import updater

addonHandler.initTranslation()

# The pending startup timer, so terminate() can stop it.
_timer = None
# Bumped by terminate(); results of checks started before that are ignored.
_generation = 0
# True while a check or download runs, so they never overlap.
_busy = False

# Results of the update dialog.
CHOICE_INSTALL = "install"
CHOICE_SKIP = "skip"
CHOICE_LATER = "later"


def _isSecure():
	return bool(getattr(globalVars.appArgs, "secure", False))


def _conf():
	from . import dialogs

	return dialogs.getConf()


def installedVersion():
	"""The version of this add-on as NVDA knows it, else from manifest.ini; None when it cannot be
	determined or parsed (then no update is ever offered).
	"""
	version = None
	try:
		version = addonHandler.getCodeAddon().version
	except Exception:
		version = None
	if not version:
		manifestPath = os.path.join(os.path.dirname(__file__), "..", "..", "manifest.ini")
		try:
			with open(manifestPath, encoding="utf-8") as f:
				version = updater.parseManifest(f.read()).get("version")
		except (OSError, ValueError):
			version = None
	version = str(version).strip() if version else None
	return version if version and updater.parseVersion(version) is not None else None


def isFromAddonStore():
	"""True when this copy was installed from the NVDA Add-on Store, which then also updates it.
	NVDA keeps store install data per add-on (AddonBase._addonStoreData, NVDA 2023.2 and later);
	it is None for add-ons installed from a file.
	"""
	try:
		addon = addonHandler.getCodeAddon()
		return getattr(addon, "_addonStoreData", None) is not None
	except Exception:
		log.debugWarning("Ask AI: could not tell whether the add-on came from the Add-on Store", exc_info=True)
		return False


def _cleanStaleDownloads():
	try:
		removed = updater.cleanStaleTempDirs()
		if removed:
			log.debug("Ask AI: removed %d old update download folders" % removed)
	except Exception:
		log.debugWarning("Ask AI: could not clean old update downloads", exc_info=True)


def scheduleAutomaticCheck():
	"""Check about 30 seconds after NVDA starts (at most once a day). Call on the main thread.
	Also removes download folders left behind by an earlier session.
	"""
	global _timer
	if _isSecure():
		return
	threading.Thread(target=_cleanStaleDownloads, name="askAI.updateCleanup", daemon=True).start()
	stopTimer()
	_timer = wx.CallLater(updater.FIRST_CHECK_DELAY_MS, _automaticCheck)


def stopTimer():
	global _timer
	timer, _timer = _timer, None
	if timer is not None:
		try:
			timer.Stop()
		except Exception:
			pass


def terminate():
	"""Stop the timer and ignore any check still running. Called when the add-on unloads."""
	global _generation, _busy
	stopTimer()
	_generation += 1
	_busy = False


def _automaticCheck():
	global _timer
	_timer = None
	if _isSecure() or _busy:
		return
	try:
		conf = _conf()
		if not conf["autoUpdateCheck"]:
			return
		if isFromAddonStore():
			log.debug("Ask AI: installed from the Add-on Store, which handles updates; skipping the GitHub check")
			return
		if not updater.shouldCheckNow(conf["lastUpdateCheck"], time.time()):
			return
	except Exception:
		log.debugWarning("Ask AI: could not start the automatic update check", exc_info=True)
		return
	_startCheck(manual=False, parent=None)


def checkForUpdates(parent=None):
	"""Manual check from the Tools menu. Call on the main thread."""
	if _isSecure():
		return
	if isFromAddonStore():
		ui.message(
			# Translators: Reported by "Check for updates" when the add-on came from the NVDA Add-on Store.
			_(
				"Ask AI was installed from the NVDA Add-on Store, which keeps it up to date. "
				"To update it, open the NVDA menu, Tools, Add-on Store, Updatable add-ons."
			)
		)
		return
	if _busy:
		# Translators: Reported when an update check or download is already running.
		ui.message(_("Already checking for Ask AI updates."))
		return
	# Translators: Announced when a manual update check starts.
	ui.message(_("Checking for Ask AI updates…"))
	_startCheck(manual=True, parent=parent)


def _startCheck(manual, parent):
	global _busy
	version = installedVersion()
	if version is None:
		log.debugWarning("Ask AI: the installed version cannot be determined; no update check")
		if manual:
			# Translators: Reported when the installed Ask AI version is unknown, so no update can be offered.
			_showError(_("Could not check for Ask AI updates: the installed version could not be determined."), parent)
		return
	_busy = True
	generation = _generation

	def worker():
		release, error = None, None
		try:
			release = updater.checkForUpdate(version)
		except updater.UpdateError as e:
			error = e
		except Exception as e:
			log.error("Ask AI: unexpected error while checking for updates", exc_info=True)
			error = updater.UpdateError(updater.KIND_INVALID, str(e))
		wx.CallAfter(_onCheckDone, generation, manual, parent, version, release, error)

	threading.Thread(target=worker, name="askAI.updateCheck", daemon=True).start()


def _liveParent(parent):
	"""The parent window if it still exists, else NVDA's main frame."""
	try:
		if parent:
			return parent
	except Exception:
		pass
	import gui

	return gui.mainFrame


def _skippedVersion():
	try:
		return _conf()["skippedUpdateVersion"]
	except Exception:
		return ""


def _onCheckDone(generation, manual, parent, version, release, error):
	global _busy
	if generation != _generation:
		return  # The add-on was unloaded meanwhile.
	_busy = False
	try:
		_conf()["lastUpdateCheck"] = float(time.time())
	except Exception:
		log.debugWarning("Ask AI: could not store the update check time", exc_info=True)
	if error is not None:
		log.debugWarning("Ask AI: update check failed: %s: %s" % (error.kind, error.message))
		if manual:
			_showError(friendlyCheckError(error), parent)
		return
	decision = updater.updateDecision(version, release, _skippedVersion(), manual)
	if decision == updater.DECISION_SKIPPED:
		log.debug("Ask AI: version %s is available but was skipped by the user" % release.version)
		return
	if decision == updater.DECISION_NONE:
		if manual:
			# Translators: Reported when no newer version exists. {version} is e.g. "1.2.0".
			ui.message(_("Ask AI {version} is the latest version.").format(version=version))
		return
	_offerUpdate(release, _liveParent(parent), wasSkipped=manual and _skippedVersion().strip() == release.version)


def _showError(message, parent):
	from . import dialogs

	dialogs.showError(message, _liveParent(parent))


def friendlyCheckError(error):
	kind = getattr(error, "kind", updater.KIND_INVALID)
	if kind == updater.KIND_NETWORK:
		# Translators: Error when GitHub cannot be reached during an update check.
		return _("Could not check for Ask AI updates. Check your internet connection and try again later.")
	# Translators: Error when the update information from GitHub could not be used.
	return _("Could not check for Ask AI updates: the release information on GitHub could not be read.")


def friendlyDownloadError(error):
	kind = getattr(error, "kind", updater.KIND_INVALID)
	if kind in (updater.KIND_CHECKSUM, updater.KIND_SIGNATURE, updater.KIND_INVALID):
		# Translators: Error when a downloaded update could not be verified (signature, checksum or contents),
		# so it was not installed.
		return _(
			"The Ask AI update was not installed because the downloaded file could not be verified. "
			"Please try again later or download it from the add-on's GitHub page."
		)
	if kind == updater.KIND_NETWORK:
		# Translators: Error when downloading an update failed or took too long.
		return _("The Ask AI update could not be downloaded. Check your internet connection and try again.")
	if kind == updater.KIND_TOO_LARGE:
		# Translators: Error when the update file is unexpectedly large.
		return _("The Ask AI update was not installed because the file is unexpectedly large.")
	# Translators: Error when the update could not be downloaded for another reason.
	return _("The Ask AI update could not be downloaded from GitHub.")


class UpdateDialog(wx.Dialog):
	"""Ask AI <version> is available: Install, What's new, Skip this version, Later (Escape)."""

	def __init__(self, parent, release, wasSkipped=False):
		# Translators: Title of the dialog offering an Ask AI update.
		super().__init__(parent, title=_("Ask AI update"))
		self.release = release
		self.choice = CHOICE_LATER
		mainSizer = wx.BoxSizer(wx.VERTICAL)
		# Translators: The update dialog's message. {version} is e.g. "1.3.0".
		text = _("Ask AI {version} is available.").format(version=release.version)
		if wasSkipped:
			# Translators: Added to the update message when the user had chosen to skip this version.
			text += " " + _("You chose to skip this version before.")
		message = wx.StaticText(self, label=text)
		mainSizer.Add(message, flag=wx.ALL, border=10)
		buttons = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Button in the update dialog that downloads, verifies and installs the update.
		self.installButton = wx.Button(self, label=_("&Install"))
		# Translators: Button in the update dialog that shows the release notes.
		notesButton = wx.Button(self, label=_("&What's new"))
		# Translators: Button in the update dialog: never offer this version automatically again.
		skipButton = wx.Button(self, label=_("&Skip this version"))
		# Translators: Button in the update dialog: ask again at a later check.
		laterButton = wx.Button(self, wx.ID_CANCEL, label=_("&Later"))
		for button in (self.installButton, notesButton, skipButton, laterButton):
			buttons.Add(button, flag=wx.ALL, border=5)
		mainSizer.Add(buttons, flag=wx.ALIGN_CENTER_HORIZONTAL)
		self.SetSizer(mainSizer)
		mainSizer.Fit(self)
		self.installButton.SetDefault()
		self.SetEscapeId(wx.ID_CANCEL)
		self.installButton.Bind(wx.EVT_BUTTON, lambda evt: self._finish(CHOICE_INSTALL))
		notesButton.Bind(wx.EVT_BUTTON, self.onNotes)
		skipButton.Bind(wx.EVT_BUTTON, lambda evt: self._finish(CHOICE_SKIP))
		laterButton.Bind(wx.EVT_BUTTON, lambda evt: self._finish(CHOICE_LATER))
		self.CentreOnScreen()

	def _finish(self, choice):
		self.choice = choice
		self.EndModal(wx.ID_OK if choice == CHOICE_INSTALL else wx.ID_CANCEL)

	def onNotes(self, evt):
		# Translators: Shown by "What's new" when the release has no notes.
		notes = self.release.notes.strip() or _("This release has no notes.")
		# Translators: Title of the window with the release notes. {version} is e.g. "1.3.0".
		ui.browseableMessage(notes, _("What's new in Ask AI {version}").format(version=self.release.version))


def askAboutUpdate(release, parent, wasSkipped=False):
	"""Show the update dialog in front (main thread). Returns CHOICE_INSTALL, CHOICE_SKIP or CHOICE_LATER."""
	import gui

	gui.mainFrame.prePopup()
	try:
		dialog = UpdateDialog(parent, release, wasSkipped)
		try:
			dialog.Raise()
			dialog.installButton.SetFocus()
			dialog.ShowModal()
			return dialog.choice
		finally:
			dialog.Destroy()
	finally:
		gui.mainFrame.postPopup()


def _offerUpdate(release, parent, wasSkipped=False):
	choice = askAboutUpdate(release, parent, wasSkipped)
	if choice == CHOICE_SKIP:
		try:
			_conf()["skippedUpdateVersion"] = release.version
		except Exception:
			log.debugWarning("Ask AI: could not store the skipped version", exc_info=True)
		# Translators: Announced after "Skip this version". {version} is e.g. "1.3.0".
		ui.message(_("Ask AI {version} will not be offered again automatically.").format(version=release.version))
		return
	if choice != CHOICE_INSTALL:
		return
	_startDownload(release, parent)


def _startDownload(release, parent):
	global _busy
	_busy = True
	generation = _generation
	# Translators: Announced when the update download starts. {version} is e.g. "1.3.0".
	ui.message(_("Downloading Ask AI {version}…").format(version=release.version))

	def worker():
		path, error, folder = None, None, None
		try:
			folder = updater.makeTempDir()
			path = updater.downloadUpdate(release, folder)
		except updater.UpdateError as e:
			error = e
		except Exception as e:
			log.error("Ask AI: unexpected error while downloading the update", exc_info=True)
			error = updater.UpdateError(updater.KIND_INVALID, str(e))
		if error is not None:
			updater.removeTempDir(folder)
			folder = None
		wx.CallAfter(_onDownloadDone, generation, parent, path, folder, error)

	threading.Thread(target=worker, name="askAI.updateDownload", daemon=True).start()


def _onDownloadDone(generation, parent, path, folder, error):
	global _busy
	if generation != _generation:
		updater.removeTempDir(folder)
		return
	_busy = False
	if error is not None:
		log.debugWarning("Ask AI: update download failed: %s: %s" % (error.kind, error.message))
		_showError(friendlyDownloadError(error), parent)
		return
	keepFile = False
	try:
		keepFile = installDownloaded(path)
	finally:
		# NVDA's installer extracts the bundle before it returns (systemUtils.ExecAndPump in
		# gui.addonGui.installAddon), so the download folder can go now. A file opened with
		# os.startfile is still needed; cleanStaleTempDirs() removes it at the next start.
		if not keepFile:
			updater.removeTempDir(folder)


def installDownloaded(path):
	"""Hand the verified file to NVDA's own add-on installation (with its confirmation and restart prompts).
	os.startfile is only used when NVDA's installer function does not exist at all; once it was called, a
	failure is reported but the file is never opened another way.
	Returns True when the file must be kept (it was opened with os.startfile).
	"""
	if _isSecure():
		return False
	try:
		from gui import addonGui

		install = addonGui.handleRemoteAddonInstall
	except Exception:
		install = None
	if install is not None:
		try:
			install(path)
		except Exception:
			log.error("Ask AI: NVDA could not install the update", exc_info=True)
			# Translators: Error when NVDA's add-on installation failed for the downloaded update.
			_showError(_("NVDA could not install the Ask AI update. Please try again later."), None)
		return False
	try:
		os.startfile(path)
		return True
	except Exception as e:
		log.error("Ask AI: could not open the downloaded update", exc_info=True)
		# Translators: Error when the downloaded update could not be opened. {path} is the file.
		_showError(_("The update was downloaded to {path} but could not be opened: {error}").format(path=path, error=e), None)
		return False
