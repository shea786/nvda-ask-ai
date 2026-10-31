# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: checks GitHub releases for a newer version, downloads it and verifies it:
# 1. every file must come from this repository's release download URL for the release's own tag;
# 2. SHA256SUMS.txt must carry a valid Ed25519 signature (SHA256SUMS.txt.sig) by the release key below;
# 3. the add-on file must match its SHA-256 in SHA256SUMS.txt;
# 4. the add-on's manifest.ini must say name = askAI and the release's version.
# Pure standard library, no NVDA imports, so it can be unit tested on any platform.
# The NVDA side (timers, prompts, installing) is in updateCheck.py.

import base64
import binascii
import glob
import hashlib
import hmac
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

try:
	from . import ed25519
except ImportError:  # Imported as a top-level module (unit tests, release tooling).
	import ed25519

REPO = "shea786/nvda-ask-gpt"
HOMEPAGE = "https://github.com/%s" % REPO
LATEST_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_URL = "https://api.github.com/repos/%s/releases?per_page=20" % REPO
ADDON_NAME = "askAI"
ADDON_SUFFIX = ".nvda-addon"
CHECKSUMS_NAME = "SHA256SUMS.txt"
SIGNATURE_NAME = "SHA256SUMS.txt.sig"
# The release signing key (Ed25519, raw 32-byte public key). The private key is kept offline by the
# maintainer (see RELEASING.md) and in the GitHub "release" environment secret ASKAI_SIGNING_KEY.
# A release whose SHA256SUMS.txt is not signed by this key is never installed.
SIGNING_PUBLIC_KEY = bytes.fromhex("aa00000b0fc84e10e2b6071c2eae9c1f2c709160c7f478d8af02deafe7a9e6ce")
# Every downloaded file must start with this, followed by the URL-quoted tag of the release and "/".
DOWNLOAD_PREFIX = "https://github.com/%s/releases/download/" % REPO
# At most one automatic check per day; the first one about 30 seconds after NVDA starts.
CHECK_INTERVAL = 24 * 3600
FIRST_CHECK_DELAY_MS = 30000
TIMEOUT = 10
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_CHECKSUMS_BYTES = 64 * 1024
MAX_SIGNATURE_BYTES = 1024
MAX_MANIFEST_BYTES = 64 * 1024
# The whole download (signature, checksums and add-on) must finish within this many seconds.
DOWNLOAD_DEADLINE = 120
CHUNK_SIZE = 64 * 1024
# Temporary download folders: tempfile.gettempdir()/askAI-update-XXXX, removed after use and at startup.
TEMP_PREFIX = "askAI-update-"
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
KIND_SIGNATURE = "signature"

# updateDecision() results.
DECISION_NONE = "none"
DECISION_OFFER = "offer"
DECISION_SKIPPED = "skipped"

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
	"""True if candidate is a newer version than installed. If either cannot be parsed, nothing is newer:
	an unknown installed version never leads to an update offer.
	"""
	if parseVersion(candidate) is None or parseVersion(installed) is None:
		return False
	return compareVersions(candidate, installed) > 0


def updateDecision(installedVersion, release, skippedVersion="", manual=False):
	"""DECISION_OFFER, DECISION_SKIPPED (the user chose "Skip this version"; automatic checks stay quiet,
	manual checks still offer it) or DECISION_NONE.
	"""
	if release is None or not isNewer(installedVersion, release.version):
		return DECISION_NONE
	skipped = (skippedVersion or "").strip()
	if skipped and parseVersion(skipped) is not None and compareVersions(skipped, release.version) == 0:
		return DECISION_OFFER if manual else DECISION_SKIPPED
	return DECISION_OFFER


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
		signatureAsset=None,
		notes="",
	):
		self.version = version
		self.tag = tag
		self.name = name
		self.htmlUrl = htmlUrl
		self.prerelease = bool(prerelease)
		self.addonAsset = addonAsset
		self.checksumsAsset = checksumsAsset
		self.draft = bool(draft)
		self.signatureAsset = signatureAsset
		# The release notes (GitHub's release "body"), shown by "What's new".
		self.notes = notes or ""

	@property
	def downloadPrefix(self):
		"""Where every file of this release must come from."""
		return releaseDownloadPrefix(self.tag)

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
	signatureAsset = next((asset for asset in assets if asset.name == SIGNATURE_NAME), None)
	name, htmlUrl, body = data.get("name"), data.get("html_url"), data.get("body")
	return Release(
		version,
		tag,
		name=name if isinstance(name, str) else "",
		htmlUrl=htmlUrl if isinstance(htmlUrl, str) else "",
		prerelease=data.get("prerelease") is True,
		addonAsset=addonAsset,
		checksumsAsset=checksumsAsset,
		draft=data.get("draft") is True,
		signatureAsset=signatureAsset,
		notes=body if isinstance(body, str) else "",
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


def releaseDownloadPrefix(tag):
	"""https://github.com/shea786/nvda-ask-gpt/releases/download/<tag>/ with the tag URL-quoted."""
	return DOWNLOAD_PREFIX + urllib.parse.quote(tag or "", safe="") + "/"


def isPinnedAssetUrl(url, tag):
	"""True when url is a download of this repository's release with exactly this tag (and an allowed host)."""
	if not isinstance(url, str) or not tag or not isAllowedDownloadUrl(url):
		return False
	prefix = releaseDownloadPrefix(tag)
	if not url.startswith(prefix):
		return False
	rest = url[len(prefix):]
	return bool(rest) and "/" not in rest and "?" not in rest and "#" not in rest and ".." not in rest


def pickUpdate(installedVersion, releases):
	"""The newest release that is newer than installedVersion, or None.
	Drafts are never offered. Prereleases (GitHub flag or version such as 1.2.0-beta1) are only offered
	when the installed version is itself a prerelease.
	"""
	if parseVersion(installedVersion) is None:
		return None
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


def _checkDeadline(deadline):
	if deadline is not None and time.monotonic() > deadline:
		raise UpdateError(KIND_NETWORK, "The download timed out after %d seconds." % DOWNLOAD_DEADLINE)


def _readCapped(response, maxBytes, deadline=None):
	length = _contentLength(response)
	if length is not None and length > maxBytes:
		raise UpdateError(KIND_TOO_LARGE, "The response is %d bytes, more than %d." % (length, maxBytes))
	chunks = []
	total = 0
	while True:
		_checkDeadline(deadline)
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
	An installed version that cannot be parsed (or None) never gets an offer and nothing is fetched.
	@raise UpdateError: network, invalid or tooLarge.
	"""
	global addonVersion
	if parseVersion(installedVersion) is None:
		return None
	addonVersion = installedVersion
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


def downloadToFile(url, path, maxBytes=MAX_DOWNLOAD_BYTES, urlopen=None, deadline=None):
	"""Stream url into path, at most maxBytes (and before deadline, a time.monotonic() value).
	Returns the sha256 hex digest. A partial file is deleted on any failure.
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
				_checkDeadline(deadline)
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


def parseSignature(data):
	"""The 64-byte signature from SHA256SUMS.txt.sig: raw bytes, or base64 text. None if neither."""
	if len(data) == 64:
		return bytes(data)
	try:
		decoded = base64.b64decode(data.strip(), validate=True)
	except (binascii.Error, ValueError):
		return None
	return decoded if len(decoded) == 64 else None


def verifyChecksumsSignature(checksumsBytes, signatureBytes, publicKey=None):
	"""True when signatureBytes (raw or base64) is a valid Ed25519 signature of the exact checksums bytes."""
	signature = parseSignature(signatureBytes)
	if signature is None:
		return False
	return ed25519.verify(publicKey if publicKey is not None else SIGNING_PUBLIC_KEY, checksumsBytes, signature)


def parseManifest(text):
	"""{key: value} from an NVDA add-on manifest.ini, without configobj: "key = value" lines, values in
	"..." or '...', triple-quoted values that may span lines, comments ("#", ";") and section lines ignored.
	@raise ValueError: for an unterminated triple-quoted value or a key given twice.
	"""
	fields = {}
	lines = text.splitlines()
	index = 0
	while index < len(lines):
		line = lines[index].strip()
		index += 1
		if not line or line[0] in "#;[" or "=" not in line:
			continue
		key, value = line.split("=", 1)
		key, value = key.strip(), value.strip()
		quote = value[:3] if value[:3] in ('"""', "'''") else None
		if quote:
			rest = value[3:]
			if quote in rest:
				value = rest[:rest.index(quote)]
			else:
				parts = [rest]
				while True:
					if index >= len(lines):
						raise ValueError("unterminated %s value for %s" % (quote, key))
					current = lines[index]
					index += 1
					if quote in current:
						parts.append(current[:current.index(quote)])
						break
					parts.append(current)
				value = "\n".join(parts)
		elif len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
			value = value[1:-1]
		else:
			value = value.split("#", 1)[0].strip()
		if key in fields:
			raise ValueError("%s is given twice" % key)
		fields[key] = value
	return fields


def checkAddonManifest(path, version, addonName=ADDON_NAME):
	"""Require a root manifest.ini with name == addonName and version == version.
	@raise UpdateError: invalid.
	"""
	try:
		with zipfile.ZipFile(path) as archive:
			try:
				info = archive.getinfo("manifest.ini")
			except KeyError:
				raise UpdateError(KIND_INVALID, "The add-on file has no manifest.ini.")
			if info.file_size > MAX_MANIFEST_BYTES:
				raise UpdateError(KIND_INVALID, "The add-on's manifest.ini is too large.")
			data = archive.read(info)
	except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as e:
		raise UpdateError(KIND_INVALID, "The add-on file is not a valid package: %s" % e)
	try:
		fields = parseManifest(data.decode("utf-8-sig"))
	except (UnicodeDecodeError, ValueError) as e:
		raise UpdateError(KIND_INVALID, "The add-on's manifest.ini cannot be read: %s" % e)
	name, manifestVersion = fields.get("name", "").strip(), fields.get("version", "").strip()
	if name != addonName or manifestVersion != (version or "").strip():
		raise UpdateError(
			KIND_INVALID,
			"The add-on file is %r version %r, expected %r version %r." % (name, manifestVersion, addonName, version),
		)


def _fetchSmall(url, maxBytes, urlopen, deadline):
	response = _open(_request(url, ALLOWED_DOWNLOAD_HOSTS, "application/octet-stream"), urlopen)
	try:
		return _readCapped(response, maxBytes, deadline)
	finally:
		_close(response)


def downloadUpdate(release, destDir, urlopen=None, publicKey=None, now=time.monotonic):
	"""Download the release's add-on into destDir and verify it (see the top of this module).
	Returns the path of the verified file; on any failure no add-on file is left in destDir.
	@raise UpdateError: signature, checksum, invalid, tooLarge or network.
	"""
	if release.checksumsAsset is None:
		raise UpdateError(KIND_CHECKSUM, "The release has no %s, so the download cannot be verified." % CHECKSUMS_NAME)
	if release.signatureAsset is None:
		raise UpdateError(KIND_SIGNATURE, "The release has no %s, so the download cannot be verified." % SIGNATURE_NAME)
	addonAsset = release.addonAsset
	fileName = safeAssetName(addonAsset.name if addonAsset is not None else "")
	if fileName is None:
		raise UpdateError(KIND_INVALID, "The release has no usable add-on file.")
	for asset in (release.signatureAsset, release.checksumsAsset, addonAsset):
		if not isPinnedAssetUrl(asset.url, release.tag):
			raise UpdateError(
				KIND_INVALID,
				"Refusing %s: it is not under %s" % (asset.url, releaseDownloadPrefix(release.tag)),
			)
	deadline = now() + DOWNLOAD_DEADLINE
	signatureBytes = _fetchSmall(release.signatureAsset.url, MAX_SIGNATURE_BYTES, urlopen, deadline)
	checksumsBytes = _fetchSmall(release.checksumsAsset.url, MAX_CHECKSUMS_BYTES, urlopen, deadline)
	# The checksums are only trusted after their signature is verified.
	if not verifyChecksumsSignature(checksumsBytes, signatureBytes, publicKey):
		raise UpdateError(KIND_SIGNATURE, "The signature of %s is not valid." % CHECKSUMS_NAME)
	expected = parseChecksums(checksumsBytes.decode("utf-8", errors="replace")).get(addonAsset.name)
	if not expected:
		raise UpdateError(KIND_CHECKSUM, "%s has no entry for %s." % (CHECKSUMS_NAME, addonAsset.name))
	path = os.path.join(destDir, fileName)
	actual = downloadToFile(addonAsset.url, path, MAX_DOWNLOAD_BYTES, urlopen, deadline)
	if not hmac.compare_digest(actual.lower(), expected.lower()):
		_removeQuietly(path)
		raise UpdateError(KIND_CHECKSUM, "Checksum mismatch for %s: expected %s, got %s." % (fileName, expected, actual))
	try:
		checkAddonManifest(path, release.version)
	except UpdateError:
		_removeQuietly(path)
		raise
	return path


# Temporary folders


def makeTempDir():
	return tempfile.mkdtemp(prefix=TEMP_PREFIX)


def removeTempDir(path):
	"""Remove a download folder made by makeTempDir (never anything else)."""
	if path and os.path.basename(os.path.normpath(path)).startswith(TEMP_PREFIX):
		shutil.rmtree(path, ignore_errors=True)


def cleanStaleTempDirs(folder=None):
	"""Remove download folders left behind (for example by a crash). Returns how many were removed."""
	folder = folder or tempfile.gettempdir()
	removed = 0
	for path in glob.glob(os.path.join(glob.escape(folder), TEMP_PREFIX + "*")):
		if os.path.isdir(path):
			shutil.rmtree(path, ignore_errors=True)
			removed += 1
	return removed
