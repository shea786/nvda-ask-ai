# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: NVDA side of the update check (timer, prompts, handing the file to NVDA's installer).
# The network and verification logic is in updater.py.

import configparser
import os
import tempfile
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


def _isSecure():
	return bool(getattr(globalVars.appArgs, "secure", False))


def _conf():
	from . import dialogs

	return dialogs.getConf()


def installedVersion():
	"""The version of this add-on as NVDA knows it, else from manifest.ini, else "0"."""
	try:
		version = addonHandler.getCodeAddon().version
		if version:
			return str(version)
	except Exception:
		pass
	manifestPath = os.path.join(os.path.dirname(__file__), "..", "..", "manifest.ini")
	try:
		parser = configparser.ConfigParser(interpolation=None)
		with open(manifestPath, encoding="utf-8") as f:
			parser.read_string("[manifest]\n" + f.read())
		return parser["manifest"]["version"].strip().strip('"') or "0"
	except Exception:
		return "0"


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


def scheduleAutomaticCheck():
	"""Check about 30 seconds after NVDA starts (at most once a day). Call on the main thread."""
	global _timer
	if _isSecure():
		return
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
	"""Manual check from the settings or the Tools menu. Call on the main thread."""
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
	_busy = True
	generation = _generation
	version = installedVersion()

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
	"""The parent window if it still exists, else None."""
	try:
		return parent if parent else None
	except Exception:
		return None


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
	if release is None:
		if manual:
			# Translators: Reported when no newer version exists. {version} is e.g. "1.2.0".
			ui.message(_("Ask AI {version} is the latest version.").format(version=version))
		return
	_offerUpdate(release, _liveParent(parent))


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
	if kind == updater.KIND_CHECKSUM:
		# Translators: Error when a downloaded update could not be verified, so it was not installed.
		return _(
			"The Ask AI update was not installed because the downloaded file could not be verified. "
			"Please try again later or download it from the add-on's GitHub page."
		)
	if kind == updater.KIND_NETWORK:
		# Translators: Error when downloading an update failed.
		return _("The Ask AI update could not be downloaded. Check your internet connection and try again.")
	if kind == updater.KIND_TOO_LARGE:
		# Translators: Error when the update file is unexpectedly large.
		return _("The Ask AI update was not installed because the file is unexpectedly large.")
	# Translators: Error when the update could not be downloaded for another reason.
	return _("The Ask AI update could not be downloaded from GitHub.")


def _offerUpdate(release, parent):
	from . import dialogs

	# Translators: Announced when a newer version exists. {version} is e.g. "1.3.0".
	ui.message(_("Ask AI {version} is available.").format(version=release.version))
	# Translators: Yes/No question when a newer version exists. {version} is e.g. "1.3.0".
	question = _("Ask AI {version} is available. Download and install now?").format(version=release.version)
	# Translators: Title of the update question.
	if not dialogs.askYesNo(question, _("Ask AI update"), parent):
		return
	_startDownload(release, parent)


def _startDownload(release, parent):
	global _busy
	_busy = True
	generation = _generation
	# Translators: Announced when the update download starts. {version} is e.g. "1.3.0".
	ui.message(_("Downloading Ask AI {version}…").format(version=release.version))

	def worker():
		path, error = None, None
		try:
			path = updater.downloadUpdate(release, tempfile.mkdtemp(prefix="askAI-update-"))
		except updater.UpdateError as e:
			error = e
		except Exception as e:
			log.error("Ask AI: unexpected error while downloading the update", exc_info=True)
			error = updater.UpdateError(updater.KIND_INVALID, str(e))
		wx.CallAfter(_onDownloadDone, generation, parent, path, error)

	threading.Thread(target=worker, name="askAI.updateDownload", daemon=True).start()


def _onDownloadDone(generation, parent, path, error):
	global _busy
	if generation != _generation:
		return
	_busy = False
	if error is not None:
		log.debugWarning("Ask AI: update download failed: %s: %s" % (error.kind, error.message))
		_showError(friendlyDownloadError(error), parent)
		return
	installDownloaded(path)


def installDownloaded(path):
	"""Hand the verified file to NVDA's own add-on installation (with its confirmation and restart prompts)."""
	if _isSecure():
		return
	try:
		from gui import addonGui

		install = addonGui.handleRemoteAddonInstall
	except Exception:
		install = None
	if install is not None:
		try:
			install(path)
			return
		except Exception:
			log.error("Ask AI: NVDA could not install the update", exc_info=True)
	try:
		os.startfile(path)
	except Exception as e:
		log.error("Ask AI: could not open the downloaded update", exc_info=True)
		# Translators: Error when the downloaded update could not be opened. {path} is the file.
		_showError(_("The update was downloaded to {path} but could not be opened: {error}").format(path=path, error=e), None)
