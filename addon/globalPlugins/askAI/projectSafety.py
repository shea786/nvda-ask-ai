# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: which folders may be a conversation's project folder.
# A project folder comes from a conversation file, which may be imported or synced (OneDrive), so it is
# never trusted: it is checked whenever it is chosen, loaded or used, by its text alone, before anything
# touches it (so a UNC path cannot make Windows connect to a server and leak NTLM credentials).
# Write permission is a separate, per-folder grant kept in the NVDA configuration (askAI.projectGrants).
# Pure standard library, no NVDA imports; the drive type check uses ctypes only on Windows.

import ctypes
import ntpath
import os
import re

DRIVE_FIXED = 3

OK = ""
REASON_EMPTY = "empty"
REASON_NOT_ABSOLUTE = "notAbsolute"
REASON_NETWORK = "network"
REASON_DRIVE_ROOT = "driveRoot"
REASON_NOT_FIXED = "notFixed"
REASON_SYSTEM = "system"
REASON_PROFILE = "profile"
REASON_APPDATA = "appData"
REASON_ADDON_DATA = "addonData"

_LOCAL_PATH = re.compile(r"^[A-Za-z]:\\")

_getDriveType = None


def _windowsDriveType(root):
	global _getDriveType
	if _getDriveType is None:
		from ctypes import wintypes

		kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
		function = kernel32.GetDriveTypeW
		function.argtypes = (wintypes.LPCWSTR,)
		function.restype = wintypes.UINT
		_getDriveType = function
	return _getDriveType(root)


def protectedFolders(env):
	"""(folder, reason) pairs that a project folder may not be inside (or equal to)."""
	result = []

	def add(path, reason):
		if path:
			result.append((path, reason))

	systemRoot = env.get("SystemRoot") or env.get("windir")
	add(systemRoot, REASON_SYSTEM)
	for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData", "ALLUSERSPROFILE"):
		add(env.get(name), REASON_SYSTEM)
	appData = env.get("APPDATA")
	localAppData = env.get("LOCALAPPDATA")
	add(appData, REASON_APPDATA)
	add(localAppData, REASON_APPDATA)
	profile = env.get("USERPROFILE")
	if profile:
		add(ntpath.join(profile, "AppData"), REASON_APPDATA)
	# Startup folders run programs at sign-in: the user's (inside APPDATA) and everyone's (inside ProgramData).
	if appData:
		add(ntpath.join(appData, "Microsoft", "Windows", "Start Menu", "Programs", "Startup"), REASON_SYSTEM)
	programData = env.get("ProgramData") or env.get("ALLUSERSPROFILE")
	if programData:
		add(ntpath.join(programData, "Microsoft", "Windows", "Start Menu", "Programs", "StartUp"), REASON_SYSTEM)
	if localAppData:
		add(ntpath.join(localAppData, "askAI"), REASON_ADDON_DATA)
	return result


def _key(path):
	return ntpath.normcase(ntpath.normpath(path)).rstrip("\\")


def _inside(path, folder):
	path, folder = _key(path), _key(folder)
	return path == folder or path.startswith(folder + "\\")


def checkFolder(path, env=None, isWindows=os.name == "nt", driveType=None):
	"""(normalised path, OK) or (None, reason), looking only at the text and the drive type."""
	env = os.environ if env is None else env
	path = (path or "").strip().strip('"')
	if not path:
		return None, REASON_EMPTY
	if not isWindows:
		# Development and tests on other systems: absolute, not the root and not the home folder itself.
		path = os.path.normpath(path)
		if not os.path.isabs(path):
			return None, REASON_NOT_ABSOLUTE
		if path == os.path.sep:
			return None, REASON_DRIVE_ROOT
		home = env.get("HOME")
		if home and os.path.normpath(home) == path:
			return None, REASON_PROFILE
		return path, OK
	if path.startswith(("\\\\", "//")) or path.startswith(("\\??\\", "\\\\?\\", "\\\\.\\")):
		return None, REASON_NETWORK
	if not _LOCAL_PATH.match(path):
		return None, REASON_NOT_ABSOLUTE
	path = ntpath.normpath(path)
	root = path[:3]
	if _key(path) == _key(root):
		return None, REASON_DRIVE_ROOT
	try:
		kind = (driveType or _windowsDriveType)(root)
	except Exception:
		kind = None
	if kind != DRIVE_FIXED:
		return None, REASON_NOT_FIXED
	profile = env.get("USERPROFILE")
	if profile and _key(path) == _key(profile):
		return None, REASON_PROFILE
	for folder, reason in protectedFolders(env):
		if _inside(path, folder):
			return None, reason
	return path, OK


def checkResolved(path, env=None, isWindows=os.name == "nt", driveType=None, realpath=os.path.realpath):
	"""checkFolder, then the same check on the resolved path (a junction may point elsewhere).
	Only call realpath after the text check passed, so nothing touches a refused path.
	"""
	folder, reason = checkFolder(path, env, isWindows, driveType)
	if reason:
		return None, reason
	try:
		resolved = realpath(folder)
	except OSError:
		return folder, OK
	if resolved and _key(resolved) != _key(folder):
		again, reason = checkFolder(resolved, env, isWindows, driveType)
		if reason:
			return None, reason
	return folder, OK


def grantKey(folder):
	"""How a folder is recorded in askAI.projectGrants: normalised, case-insensitive on Windows."""
	return ntpath.normcase(ntpath.normpath(folder)) if os.name == "nt" or ":" in folder[:3] else os.path.normpath(folder)


def hasGrant(grants, folder):
	key = grantKey(folder)
	return any(grantKey(item) == key for item in grants or ())


def addGrant(grants, folder):
	grants = list(grants or ())
	if not hasGrant(grants, folder):
		grants.append(grantKey(folder))
	return grants
