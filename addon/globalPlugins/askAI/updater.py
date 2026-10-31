# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: checks GitHub releases for a newer version, downloads it and verifies its checksum.
# Pure standard library, no NVDA imports, so it can be unit tested on any platform.
# The NVDA side (timers, prompts, installing) is in updateCheck.py.

import hashlib
import hmac
import http.client
import json
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

REPO = "shea786/nvda-ask-gpt"
HOMEPAGE = "https://github.com/%s" % REPO
LATEST_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_URL = "https://api.github.com/repos/%s/releases?per_page=20" % REPO
ADDON_NAME = "askAI"
ADDON_SUFFIX = ".nvda-addon"
CHECKSUMS_NAME = "SHA256SUMS.txt"
# At most one automatic check per day; the first one about 30 seconds after NVDA starts.
CHECK_INTERVAL = 24 * 3600
FIRST_CHECK_DELAY_MS = 30000
TIMEOUT = 10
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_CHECKSUMS_BYTES = 64 * 1024
CHUNK_SIZE = 64 * 1024
API_HOSTS = ("api.github.com",)
# Release assets are served from github.com, which redirects to one of the other two.
ALLOWED_DOWNLOAD_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")
USER_AGENT = "askAI-NVDA-addon/%s (+" + HOMEPAGE + ")"
# Set by the NVDA side so the User-Agent names the installed version.
addonVersion = ""

# Error kinds.
KIND_NETWORK = "network"
KIND_INVALID = "invalid"
KIND_CHECKSUM = "checksum"
KIND_TOO_LARGE = "tooLarge"
KIND_NOT_FOUND = "notFound"

_VERSION = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z][0-9A-Za-z.-]*))?$")
_IDENTIFIER_PART = re.compile(r"\d+|[A-Za-z]+")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_ASSET_NAME = re.compile(r"^[A-Za-z0-9._-]+\.nvda-addon$")


class UpdateError(Exception):
	"""A failure checking for or downloading an update.
	@ivar kind: one of the KIND_* constants.
	@ivar message: an English message for the log; the NVDA side shows translated text per kind.
	"""

	def __init__(self, kind, message):
		super().__init__(message)
		self.kind = kind
		self.message = message

	def __str__(self):
		return self.message


# Versions


def _prereleaseKey(text):
	"""Comparison key for a prerelease such as "beta1" or "rc.2": numbers sort before words, as in semver."""
	key = []
	for identifier in text.split("."):
		for part in _IDENTIFIER_PART.findall(identifier):
			key.append((0, int(part), "") if part.isdigit() else (1, 0, part.lower()))
	return tuple(key)


def parseVersion(text):
	"""(major, minor, patch, prerelease key) for "1.2", "v1.2.0" or "1.2.0-beta1", or None if not a version.
	The prerelease key is () for a release.
	"""
	match = _VERSION.match((text or "").strip())
	if not match:
		return None
	major, minor, patch, prerelease = match.groups()
	return (int(major), int(minor or 0), int(patch or 0), _prereleaseKey(prerelease) if prerelease else ())


def isPrerelease(text):
	parsed = parseVersion(text)
	return parsed is not None and bool(parsed[3])


def _sortKey(parsed):
	major, minor, patch, prerelease = parsed
	# A release sorts after every prerelease of the same version.
	return (major, minor, patch, 0 if prerelease else 1, prerelease)


def compareVersions(a, b):
	"""-1, 0 or 1. Raises ValueError if either is not a version."""
	parsedA, parsedB = parseVersion(a), parseVersion(b)
	if parsedA is None or parsedB is None:
		raise ValueError("not a version: %r / %r" % (a, b))
	keyA, keyB = _sortKey(parsedA), _sortKey(parsedB)
	return (keyA > keyB) - (keyA < keyB)


def isNewer(installed, candidate):
	"""True if candidate is a newer version than installed. An unparsable installed version is treated as 0."""
	if parseVersion(candidate) is None:
		return False
	if parseVersion(installed) is None:
		installed = "0"
	return compareVersions(candidate, installed) > 0


# Releases


class Asset:
	def __init__(self, name, url, size=0):
		self.name = name
		self.url = url
		self.size = size

	def __repr__(self):
		return "Asset(%r, %r, %r)" % (self.name, self.url, self.size)


class Release:
	def __init__(
		self,
		version,
		tag,
		name="",
		htmlUrl="",
		prerelease=False,
		addonAsset=None,
		checksumsAsset=None,
		draft=False,
	):
		self.version = version
		self.tag = tag
		self.name = name
		self.htmlUrl = htmlUrl
		self.prerelease = bool(prerelease)
		self.addonAsset = addonAsset
		self.checksumsAsset = checksumsAsset
		self.draft = bool(draft)

	@property
	def isPrerelease(self):
		"""Marked as a prerelease on GitHub, or a version with a prerelease part such as 1.2.0-beta1."""
		return self.prerelease or isPrerelease(self.version)

	def __repr__(self):
		return "Release(%r, prerelease=%r, draft=%r)" % (self.version, self.prerelease, self.draft)


def _assets(data):
	assets = []
	for entry in data.get("assets") or []:
		if not isinstance(entry, dict):
			continue
		name, url = entry.get("name"), entry.get("browser_download_url")
		if not isinstance(name, str) or not isinstance(url, str) or not name or not url:
			continue
		size = entry.get("size")
		assets.append(Asset(name, url, size if isinstance(size, int) and size >= 0 else 0))
	return assets


def selectAsset(assets, version, addonName=ADDON_NAME):
	"""The add-on file of a release: "<addonName>-<version>.nvda-addon", else the only
	"<addonName>-*.nvda-addon". None when there is none or the choice is ambiguous.
	"""
	exact = "%s-%s%s" % (addonName, version, ADDON_SUFFIX)
	for asset in assets:
		if asset.name == exact:
			return asset
	candidates = [
		asset for asset in assets if asset.name.startswith(addonName + "-") and asset.name.endswith(ADDON_SUFFIX)
	]
	return candidates[0] if len(candidates) == 1 else None


def parseRelease(data, addonName=ADDON_NAME):
	"""A Release from GitHub's release JSON.
	@raise UpdateError: invalid, when it has no usable tag or no add-on file.
	"""
	if not isinstance(data, dict):
		raise UpdateError(KIND_INVALID, "The release information is not a JSON object.")
	tag = data.get("tag_name")
	if not isinstance(tag, str) or parseVersion(tag) is None:
		raise UpdateError(KIND_INVALID, "The release has no version tag: %r" % (tag,))
	version = tag.strip()
	if version[:1] in ("v", "V"):
		version = version[1:]
	assets = _assets(data)
	addonAsset = selectAsset(assets, version, addonName)
	if addonAsset is None:
		raise UpdateError(KIND_INVALID, "Release %s has no %s-%s%s file." % (tag, addonName, version, ADDON_SUFFIX))
	checksumsAsset = next((asset for asset in assets if asset.name == CHECKSUMS_NAME), None)
	name, htmlUrl = data.get("name"), data.get("html_url")
	return Release(
		version,
		tag,
		name=name if isinstance(name, str) else "",
		htmlUrl=htmlUrl if isinstance(htmlUrl, str) else "",
		prerelease=data.get("prerelease") is True,
		addonAsset=addonAsset,
		checksumsAsset=checksumsAsset,
		draft=data.get("draft") is True,
	)


def parseReleases(dataList, addonName=ADDON_NAME):
	"""Releases from GitHub's release list JSON, skipping entries that are not usable."""
	releases = []
	for data in dataList if isinstance(dataList, list) else []:
		try:
			releases.append(parseRelease(data, addonName))
		except UpdateError:
			continue
	return releases


def pickUpdate(installedVersion, releases):
	"""The newest release that is newer than installedVersion, or None.
	Drafts are never offered. Prereleases (GitHub flag or version such as 1.2.0-beta1) are only offered
	when the installed version is itself a prerelease.
	"""
	allowPrereleases = isPrerelease(installedVersion)
	best = None
	for release in releases:
		if release.draft or (release.isPrerelease and not allowPrereleases):
			continue
		if not isNewer(installedVersion, release.version):
			continue
		if best is None or compareVersions(release.version, best.version) > 0:
			best = release
	return best


# Network


def _hostOf(url):
	try:
		parts = urllib.parse.urlsplit(url)
		port = parts.port
	except ValueError:
		return None
	if parts.scheme != "https" or parts.username is not None or parts.password is not None:
		return None
	if port not in (None, 443):
		return None
	return (parts.hostname or "").lower()


def isAllowedUrl(url, hosts):
	"""https, no user name or password, default port, and a host exactly in hosts."""
	host = _hostOf(url)
	return bool(host) and host in hosts


def isAllowedDownloadUrl(url):
	return isAllowedUrl(url, ALLOWED_DOWNLOAD_HOSTS)


class RestrictedRedirectHandler(urllib.request.HTTPRedirectHandler):
	"""Follows redirects only to https addresses on the hosts allowed for the request
	(GitHub repository renames for API calls, the asset storage hosts for downloads).
	"""

	def redirect_request(self, req, fp, code, msg, headers, newurl):
		hosts = getattr(req, "allowedHosts", ())
		newurl = urllib.parse.urljoin(req.full_url, newurl)
		if not isAllowedUrl(newurl, hosts):
			raise urllib.error.HTTPError(newurl, code, "Redirect to a host that is not allowed: %s" % newurl, headers, fp)
		newRequest = super().redirect_request(req, fp, code, msg, headers, newurl)
		if newRequest is not None:
			newRequest.allowedHosts = hosts
		return newRequest


def buildOpener():
	"""An opener with certificate checking (the default SSL context) and restricted redirects."""
	return urllib.request.build_opener(
		urllib.request.HTTPSHandler(context=ssl.create_default_context()),
		RestrictedRedirectHandler(),
	)


def _defaultUrlopen(request, timeout=TIMEOUT):
	return buildOpener().open(request, timeout=timeout)


def userAgent():
	return USER_AGENT % (addonVersion or "0")


def _request(url, hosts, accept):
	if not isAllowedUrl(url, hosts):
		raise UpdateError(KIND_INVALID, "Refusing to download from %s" % url)
	request = urllib.request.Request(url, headers={"User-Agent": userAgent(), "Accept": accept})
	request.allowedHosts = tuple(hosts)
	return request


def _open(request, urlopen):
	"""Open a request and check where it ended up. Maps failures to UpdateError."""
	try:
		response = (urlopen or _defaultUrlopen)(request, timeout=TIMEOUT)
	except urllib.error.HTTPError as e:
		if e.code == 404:
			raise UpdateError(KIND_NOT_FOUND, "Not found: %s" % request.full_url)
		if 300 <= e.code < 400:
			raise UpdateError(KIND_INVALID, "Refused redirect from %s: %s" % (request.full_url, e.msg))
		raise UpdateError(KIND_NETWORK, "HTTP error %s for %s: %s" % (e.code, request.full_url, e.reason))
	except (urllib.error.URLError, http.client.HTTPException, socket.timeout, ssl.SSLError, OSError, ValueError) as e:
		raise UpdateError(KIND_NETWORK, "Could not reach %s: %s" % (request.full_url, e))
	finalUrl = getattr(response, "geturl", lambda: request.full_url)() or request.full_url
	if not isAllowedUrl(finalUrl, request.allowedHosts):
		_close(response)
		raise UpdateError(KIND_INVALID, "The download ended up on a host that is not allowed: %s" % finalUrl)
	return response


def _close(response):
	try:
		response.close()
	except Exception:
		pass


def _contentLength(response):
	headers = getattr(response, "headers", None)
	value = headers.get("Content-Length") if headers is not None else None
	try:
		return int(value) if value is not None else None
	except (TypeError, ValueError):
		return None


def _readCapped(response, maxBytes):
	length = _contentLength(response)
	if length is not None and length > maxBytes:
		raise UpdateError(KIND_TOO_LARGE, "The response is %d bytes, more than %d." % (length, maxBytes))
	chunks = []
	total = 0
	while True:
		try:
			chunk = response.read(CHUNK_SIZE)
		except (http.client.HTTPException, socket.timeout, OSError) as e:
			raise UpdateError(KIND_NETWORK, "The download was interrupted: %s" % e)
		if not chunk:
			break
		total += len(chunk)
		if total > maxBytes:
			raise UpdateError(KIND_TOO_LARGE, "The response is larger than %d bytes." % maxBytes)
		chunks.append(chunk)
	return b"".join(chunks)


def fetchJson(url, urlopen=None, maxBytes=MAX_JSON_BYTES):
	"""Decoded JSON from the GitHub API.
	@raise UpdateError: network, notFound, tooLarge or invalid.
	"""
	response = _open(_request(url, API_HOSTS, "application/vnd.github+json"), urlopen)
	try:
		data = _readCapped(response, maxBytes)
	finally:
		_close(response)
	try:
		return json.loads(data.decode("utf-8"))
	except (UnicodeDecodeError, ValueError) as e:
		raise UpdateError(KIND_INVALID, "GitHub returned data that is not JSON: %s" % e)


def checkForUpdate(installedVersion, urlopen=None):
	"""The release to offer, or None when this is the newest version (or nothing is published yet).
	A prerelease installation looks at the recent releases including prereleases; otherwise only at
	GitHub's latest release, which never is a prerelease or a draft.
	@raise UpdateError: network, invalid or tooLarge.
	"""
	global addonVersion
	addonVersion = installedVersion or addonVersion
	try:
		if isPrerelease(installedVersion):
			releases = parseReleases(fetchJson(RELEASES_URL, urlopen))
		else:
			releases = [parseRelease(fetchJson(LATEST_URL, urlopen))]
	except UpdateError as e:
		if e.kind == KIND_NOT_FOUND:
			return None  # No release published yet.
		raise
	return pickUpdate(installedVersion, releases)


def shouldCheckNow(lastCheck, now, interval=CHECK_INTERVAL):
	"""True when the last automatic check is at least interval seconds ago, never happened,
	or lies more than a day in the future (the clock was changed).
	"""
	try:
		lastCheck = float(lastCheck or 0)
	except (TypeError, ValueError):
		return True
	if lastCheck <= 0:
		return True
	if lastCheck - now > 24 * 3600:
		return True
	return now - lastCheck >= interval


# Checksums and downloads


def parseChecksums(text):
	"""{file name: lowercase sha256} from sha256sum output ("<hex>  <name>" or "<hex> *<name>")."""
	result = {}
	for rawLine in (text or "").splitlines():
		line = rawLine.strip()
		if not line or line.startswith("#"):
			continue
		parts = line.split(None, 1)
		if len(parts) != 2 or not _HEX64.match(parts[0]):
			continue
		name = parts[1].strip()
		if name.startswith("*"):
			name = name[1:]
		if name:
			result[name] = parts[0].lower()
	return result


def _removeQuietly(path):
	try:
		os.remove(path)
	except OSError:
		pass


def downloadToFile(url, path, maxBytes=MAX_DOWNLOAD_BYTES, urlopen=None):
	"""Stream url into path, at most maxBytes. Returns the sha256 hex digest.
	A partial file is deleted on any failure.
	"""
	response = _open(_request(url, ALLOWED_DOWNLOAD_HOSTS, "application/octet-stream"), urlopen)
	digest = hashlib.sha256()
	try:
		length = _contentLength(response)
		if length is not None and length > maxBytes:
			raise UpdateError(KIND_TOO_LARGE, "The file is %d bytes, more than %d." % (length, maxBytes))
		total = 0
		with open(path, "wb") as f:
			while True:
				try:
					chunk = response.read(CHUNK_SIZE)
				except (http.client.HTTPException, socket.timeout, OSError) as e:
					raise UpdateError(KIND_NETWORK, "The download was interrupted: %s" % e)
				if not chunk:
					break
				total += len(chunk)
				if total > maxBytes:
					raise UpdateError(KIND_TOO_LARGE, "The file is larger than %d bytes." % maxBytes)
				digest.update(chunk)
				f.write(chunk)
	except BaseException:
		_close(response)
		_removeQuietly(path)
		raise
	_close(response)
	return digest.hexdigest()


def safeAssetName(name):
	"""The asset name as a plain file name ending in .nvda-addon, or None."""
	base = os.path.basename((name or "").replace("\\", "/"))
	return base if _SAFE_ASSET_NAME.match(base) and not base.startswith(".") else None


def downloadUpdate(release, destDir, urlopen=None):
	"""Download the release's add-on into destDir and verify it against the release's SHA256SUMS.txt.
	Returns the path of the verified file.
	@raise UpdateError: checksum (no checksum file, no entry, or mismatch), invalid, tooLarge or network.
	"""
	if release.checksumsAsset is None:
		raise UpdateError(KIND_CHECKSUM, "The release has no %s, so the download cannot be verified." % CHECKSUMS_NAME)
	addonAsset = release.addonAsset
	fileName = safeAssetName(addonAsset.name if addonAsset is not None else "")
	if fileName is None:
		raise UpdateError(KIND_INVALID, "The release has no usable add-on file.")
	for asset in (release.checksumsAsset, addonAsset):
		if not isAllowedDownloadUrl(asset.url):
			raise UpdateError(KIND_INVALID, "Refusing to download from %s" % asset.url)
	response = _open(_request(release.checksumsAsset.url, ALLOWED_DOWNLOAD_HOSTS, "application/octet-stream"), urlopen)
	try:
		checksumsText = _readCapped(response, MAX_CHECKSUMS_BYTES).decode("utf-8", errors="replace")
	finally:
		_close(response)
	expected = parseChecksums(checksumsText).get(addonAsset.name)
	if not expected:
		raise UpdateError(KIND_CHECKSUM, "%s has no entry for %s." % (CHECKSUMS_NAME, addonAsset.name))
	path = os.path.join(destDir, fileName)
	actual = downloadToFile(addonAsset.url, path, MAX_DOWNLOAD_BYTES, urlopen)
	if not hmac.compare_digest(actual.lower(), expected.lower()):
		_removeQuietly(path)
		raise UpdateError(KIND_CHECKSUM, "Checksum mismatch for %s: expected %s, got %s." % (fileName, expected, actual))
	return path
