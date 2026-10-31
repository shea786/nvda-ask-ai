"""Tests for updater (no NVDA, no network: urlopen is always mocked)."""

import email.message
import hashlib
import io
import json
import os
import py_compile
import shutil
import socket
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest import mock

import helpers

_PACKAGE = os.path.join(helpers.ROOT, "addon", "globalPlugins", "askAI")
if _PACKAGE not in sys.path:
	sys.path.insert(0, _PACKAGE)

import ed25519  # noqa: E402
import updater  # noqa: E402
from updater import Asset, Release, UpdateError  # noqa: E402

# A test signing key (never the release key); its public key replaces updater.SIGNING_PUBLIC_KEY in tests.
TEST_SECRET = bytes(range(32))
TEST_PUBLIC = ed25519.publicKey(TEST_SECRET)


def makeAddon(manifest='name = askAI\nsummary = "Ask AI"\nversion = 1.3.0\n', extra=()):
	buffer = io.BytesIO()
	with zipfile.ZipFile(buffer, "w") as archive:
		if manifest is not None:
			archive.writestr("manifest.ini", manifest)
		archive.writestr("globalPlugins/askAI/__init__.py", "# code\n")
		for name, data in extra:
			archive.writestr(name, data)
	return buffer.getvalue()


ADDON_BYTES = makeAddon()
ADDON_SHA = hashlib.sha256(ADDON_BYTES).hexdigest()
DL = "https://github.com/shea786/nvda-ask-gpt/releases/download"


def releaseJson(tag="v1.3.0", prerelease=False, draft=False, assets=None, version=None):
	version = version or tag.lstrip("v")
	if assets is None:
		assets = [
			{"name": "askAI-%s.nvda-addon" % version, "browser_download_url": "%s/%s/askAI-%s.nvda-addon" % (DL, tag, version), "size": len(ADDON_BYTES)},
			{"name": "SHA256SUMS.txt", "browser_download_url": "%s/%s/SHA256SUMS.txt" % (DL, tag), "size": 100},
			{"name": "SHA256SUMS.txt.sig", "browser_download_url": "%s/%s/SHA256SUMS.txt.sig" % (DL, tag), "size": 64},
			{"name": "TESTING.txt", "browser_download_url": "%s/%s/TESTING.txt" % (DL, tag), "size": 100},
		]
	return {
		"body": "Release notes for %s." % version,
		"tag_name": tag,
		"name": "Ask AI %s" % version,
		"html_url": "https://github.com/shea786/nvda-ask-gpt/releases/tag/%s" % tag,
		"prerelease": prerelease,
		"draft": draft,
		"assets": assets,
	}


class FakeResponse(io.BytesIO):
	def __init__(self, data, url, contentLength="auto"):
		super().__init__(data)
		self.url = url
		self.headers = email.message.Message()
		if contentLength == "auto":
			self.headers["Content-Length"] = str(len(data))
		elif contentLength is not None:
			self.headers["Content-Length"] = str(contentLength)

	def geturl(self):
		return self.url


class FakeUrlopen:
	"""Serves bytes per URL; records requests and timeouts."""

	def __init__(self, routes):
		self.routes = routes
		self.requests = []

	def __call__(self, request, timeout=None):
		self.requests.append((request, timeout))
		route = self.routes.get(request.full_url)
		if route is None:
			raise urllib.error.HTTPError(request.full_url, 404, "Not Found", email.message.Message(), None)
		if isinstance(route, BaseException):
			raise route
		if callable(route):
			return route(request)
		return FakeResponse(route, request.full_url)


def downloadRoutes(release, checksums=None, addon=ADDON_BYTES, signature=None, secret=TEST_SECRET):
	if checksums is None:
		checksums = "%s  %s\n" % (hashlib.sha256(addon).hexdigest(), release.addonAsset.name)
	data = checksums.encode("utf-8")
	if signature is None:
		signature = ed25519.sign(secret, data)
	return {
		release.signatureAsset.url: signature,
		release.checksumsAsset.url: data,
		release.addonAsset.url: addon,
	}


class TestVersions(unittest.TestCase):
	def test_parse(self):
		self.assertEqual(updater.parseVersion("1.2.0"), (1, 2, 0, ()))
		self.assertEqual(updater.parseVersion("v1.2"), (1, 2, 0, ()))
		self.assertEqual(updater.parseVersion("2"), (2, 0, 0, ()))
		self.assertEqual(updater.parseVersion("1.2.0-beta1")[:3], (1, 2, 0))
		self.assertTrue(updater.parseVersion("1.2.0-rc.2")[3])
		for bad in ("", None, "abc", "1.2.0.0", "1.x", "v", "1.2.0-", "-1.2"):
			self.assertIsNone(updater.parseVersion(bad), bad)

	def test_order(self):
		ordered = ["1.1.9", "1.2.0-alpha", "1.2.0-beta1", "1.2.0-beta2", "1.2.0-beta10", "1.2.0-rc1", "1.2.0", "1.2.1", "1.10.0", "2.0"]
		for older, newer in zip(ordered, ordered[1:]):
			with self.subTest(older=older, newer=newer):
				self.assertEqual(updater.compareVersions(older, newer), -1)
				self.assertEqual(updater.compareVersions(newer, older), 1)
		self.assertEqual(updater.compareVersions("v1.2", "1.2.0"), 0)
		with self.assertRaises(ValueError):
			updater.compareVersions("x", "1.0")

	def test_prereleaseAndNewer(self):
		self.assertTrue(updater.isPrerelease("1.2.0-beta1"))
		self.assertFalse(updater.isPrerelease("1.2.0"))
		self.assertTrue(updater.isNewer("1.2.0", "1.2.1"))
		self.assertFalse(updater.isNewer("1.2.0", "1.2.0"))
		self.assertFalse(updater.isNewer("1.2.0", "nonsense"))
		# L4: an installed version that cannot be parsed is never offered an update (not treated as "0").
		self.assertFalse(updater.isNewer("garbage", "1.0.0"))
		self.assertFalse(updater.isNewer(None, "1.0.0"))
		self.assertIsNone(updater.pickUpdate("garbage", updater.parseReleases([releaseJson("v9.0.0")])))

	def test_updateDecision(self):
		release = updater.parseRelease(releaseJson("v1.3.0"))
		self.assertEqual(updater.updateDecision("1.2.0", release), updater.DECISION_OFFER)
		self.assertEqual(updater.updateDecision("1.3.0", release), updater.DECISION_NONE)
		self.assertEqual(updater.updateDecision("1.2.0", None), updater.DECISION_NONE)
		self.assertEqual(updater.updateDecision(None, release), updater.DECISION_NONE)
		self.assertEqual(updater.updateDecision("1.2.0", release, "1.3.0"), updater.DECISION_SKIPPED, "automatic: skipped")
		self.assertEqual(updater.updateDecision("1.2.0", release, "v1.3.0"), updater.DECISION_SKIPPED)
		self.assertEqual(updater.updateDecision("1.2.0", release, "1.3.0", manual=True), updater.DECISION_OFFER)
		self.assertEqual(updater.updateDecision("1.2.0", release, "1.2.5"), updater.DECISION_OFFER, "an older skip does not hide 1.3.0")
		self.assertEqual(updater.updateDecision("1.2.0", release, "junk"), updater.DECISION_OFFER)


class TestReleases(unittest.TestCase):
	def test_parseRelease(self):
		release = updater.parseRelease(releaseJson("v1.3.0"))
		self.assertEqual((release.version, release.tag), ("1.3.0", "v1.3.0"))
		self.assertEqual(release.addonAsset.name, "askAI-1.3.0.nvda-addon")
		self.assertEqual(release.checksumsAsset.name, "SHA256SUMS.txt")
		self.assertEqual(release.signatureAsset.name, "SHA256SUMS.txt.sig")
		self.assertEqual(release.notes, "Release notes for 1.3.0.")
		self.assertEqual(release.downloadPrefix, DL + "/v1.3.0/")
		self.assertFalse(release.isPrerelease)
		self.assertTrue(updater.parseRelease(releaseJson("v1.3.0-beta1")).isPrerelease)
		self.assertTrue(updater.parseRelease(releaseJson("v1.3.0", prerelease=True)).isPrerelease)

	def test_invalidReleases(self):
		for data in ([], {"assets": []}, {"tag_name": "latest"}, releaseJson(assets=[])):
			with self.assertRaises(UpdateError) as ctx:
				updater.parseRelease(data)
			self.assertEqual(ctx.exception.kind, "invalid")

	def test_selectAsset(self):
		def asset(name):
			return Asset(name, "%s/v1/%s" % (DL, name))

		exact = updater.selectAsset([asset("askAI-1.3.0.nvda-addon"), asset("askAI-old.nvda-addon")], "1.3.0")
		self.assertEqual(exact.name, "askAI-1.3.0.nvda-addon")
		self.assertEqual(updater.selectAsset([asset("askAI-1.3.nvda-addon")], "1.3.0").name, "askAI-1.3.nvda-addon")
		self.assertIsNone(updater.selectAsset([asset("askAI-a.nvda-addon"), asset("askAI-b.nvda-addon")], "1.3.0"))
		self.assertIsNone(updater.selectAsset([asset("askGPT-1.3.0.nvda-addon"), asset("askAI-1.3.0.zip")], "1.3.0"))
		self.assertIsNone(updater.selectAsset([asset("otherAskAI-1.3.0.nvda-addon")], "1.3.0"))

	def test_missingChecksumsAsset(self):
		data = releaseJson()
		data["assets"] = [a for a in data["assets"] if a["name"] != "SHA256SUMS.txt"]
		self.assertIsNone(updater.parseRelease(data).checksumsAsset)

	def test_pickUpdateForStableInstall(self):
		releases = updater.parseReleases(
			[
				releaseJson("v1.4.0-beta1"),
				releaseJson("v1.5.0", prerelease=True),
				releaseJson("v1.6.0", draft=True),
				releaseJson("v1.3.0"),
				releaseJson("v1.1.0"),
				{"tag_name": "broken"},
			],
		)
		self.assertEqual(updater.pickUpdate("1.2.0", releases).version, "1.3.0")
		self.assertIsNone(updater.pickUpdate("1.3.0", releases))

	def test_pickUpdateForPrereleaseInstall(self):
		releases = updater.parseReleases(
			[releaseJson("v1.2.0-beta2"), releaseJson("v1.2.0-beta1"), releaseJson("v1.1.0"), releaseJson("v1.3.0", draft=True)],
		)
		self.assertEqual(updater.pickUpdate("1.2.0-beta1", releases).version, "1.2.0-beta2")
		releases += updater.parseReleases([releaseJson("v1.2.0")])
		self.assertEqual(updater.pickUpdate("1.2.0-beta1", releases).version, "1.2.0")


class TestUrls(unittest.TestCase):
	def test_allowList(self):
		for url in (
			DL + "/v1.2.0/askAI-1.2.0.nvda-addon",
			"https://objects.githubusercontent.com/github-production-release-asset/x",
			"https://release-assets.githubusercontent.com/github-production-release-asset/x?sp=r",
			"https://GITHUB.com:443/x",
		):
			self.assertTrue(updater.isAllowedDownloadUrl(url), url)
		for url in (
			"http://github.com/x",
			"https://github.com.evil.com/x",
			"https://evilgithub.com/x",
			"https://user:pw@github.com/x",
			"https://github.com@evil.com/x",
			"https://github.com:8443/x",
			"https://api.github.com/x",
			"ftp://github.com/x",
			"https://raw.githubusercontent.com/x",
			"",
		):
			self.assertFalse(updater.isAllowedDownloadUrl(url), url)

	def _redirect(self, fromUrl, toUrl, hosts):
		handler = updater.RestrictedRedirectHandler()
		request = urllib.request.Request(fromUrl)
		request.allowedHosts = hosts
		return handler.redirect_request(request, io.BytesIO(b""), 302, "Found", email.message.Message(), toUrl)

	def test_redirects(self):
		new = self._redirect(DL + "/v1/a.nvda-addon", "https://release-assets.githubusercontent.com/a", updater.ALLOWED_DOWNLOAD_HOSTS)
		self.assertEqual(new.full_url, "https://release-assets.githubusercontent.com/a")
		self.assertEqual(new.allowedHosts, updater.ALLOWED_DOWNLOAD_HOSTS)
		# A renamed repository: the API answers with a redirect to /repositories/<id>/...
		new = self._redirect(updater.LATEST_URL, "https://api.github.com/repositories/123/releases/latest", updater.API_HOSTS)
		self.assertEqual(new.full_url, "https://api.github.com/repositories/123/releases/latest")
		for target, hosts in (
			("https://evil.example/a", updater.ALLOWED_DOWNLOAD_HOSTS),
			("http://release-assets.githubusercontent.com/a", updater.ALLOWED_DOWNLOAD_HOSTS),
			("https://github.com/x", updater.API_HOSTS),
			("https://api.github.com/x", updater.ALLOWED_DOWNLOAD_HOSTS),
		):
			with self.subTest(target=target):
				with self.assertRaises(urllib.error.HTTPError):
					self._redirect("https://github.com/a", target, hosts)

	def test_openerUsesVerifiedTls(self):
		opener = updater.buildOpener()
		handlers = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
		self.assertTrue(handlers)
		context = handlers[0]._context
		self.assertTrue(context.check_hostname)
		self.assertTrue(any(isinstance(h, updater.RestrictedRedirectHandler) for h in opener.handlers))


class TestFetch(unittest.TestCase):
	def test_headersAndTimeout(self):
		urlopen = FakeUrlopen({updater.LATEST_URL: json.dumps(releaseJson()).encode()})
		updater.addonVersion = "1.2.0"
		self.assertEqual(updater.fetchJson(updater.LATEST_URL, urlopen)["tag_name"], "v1.3.0")
		request, timeout = urlopen.requests[0]
		self.assertEqual(timeout, 10)
		self.assertEqual(request.get_header("User-agent"), "askAI-NVDA-addon/1.2.0 (+https://github.com/shea786/nvda-ask-gpt)")
		self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")

	def test_errors(self):
		cases = (
			(urllib.error.URLError("no route"), "network"),
			(socket.timeout("timed out"), "network"),
			(ConnectionResetError(), "network"),
			(urllib.error.HTTPError(updater.LATEST_URL, 500, "boom", email.message.Message(), None), "network"),
		)
		for error, kind in cases:
			with self.subTest(error=error):
				with self.assertRaises(UpdateError) as ctx:
					updater.fetchJson(updater.LATEST_URL, FakeUrlopen({updater.LATEST_URL: error}))
				self.assertEqual(ctx.exception.kind, kind)
		with self.assertRaises(UpdateError) as ctx:
			updater.fetchJson(updater.LATEST_URL, FakeUrlopen({}))
		self.assertEqual(ctx.exception.kind, "notFound")
		with self.assertRaises(UpdateError) as ctx:
			updater.fetchJson(updater.LATEST_URL, FakeUrlopen({updater.LATEST_URL: b"<html>"}))
		self.assertEqual(ctx.exception.kind, "invalid")
		big = lambda request: FakeResponse(b"{}", request.full_url, contentLength=updater.MAX_JSON_BYTES + 1)
		with self.assertRaises(UpdateError) as ctx:
			updater.fetchJson(updater.LATEST_URL, FakeUrlopen({updater.LATEST_URL: big}))
		self.assertEqual(ctx.exception.kind, "tooLarge")

	def test_refusesNonApiUrlAndOffListFinalUrl(self):
		with self.assertRaises(UpdateError):
			updater.fetchJson("https://evil.example/releases", FakeUrlopen({}))
		moved = lambda request: FakeResponse(b"{}", "https://evil.example/x")
		with self.assertRaises(UpdateError) as ctx:
			updater.fetchJson(updater.LATEST_URL, FakeUrlopen({updater.LATEST_URL: moved}))
		self.assertEqual(ctx.exception.kind, "invalid")

	def test_checkForUpdateStable(self):
		urlopen = FakeUrlopen({updater.LATEST_URL: json.dumps(releaseJson("v1.3.0")).encode()})
		release = updater.checkForUpdate("1.2.0", urlopen)
		self.assertEqual(release.version, "1.3.0")
		self.assertEqual([r.full_url for r, _t in urlopen.requests], [updater.LATEST_URL])
		self.assertIsNone(updater.checkForUpdate("1.3.0", urlopen))
		self.assertIsNone(updater.checkForUpdate("1.2.0", FakeUrlopen({})), "no release published yet")
		unknown = FakeUrlopen({updater.LATEST_URL: json.dumps(releaseJson("v9.0.0")).encode()})
		self.assertIsNone(updater.checkForUpdate(None, unknown))
		self.assertIsNone(updater.checkForUpdate("not a version", unknown))
		self.assertEqual(unknown.requests, [], "nothing is fetched for an unknown installed version")

	def test_checkForUpdatePrerelease(self):
		data = [releaseJson("v1.2.0-beta2", prerelease=True), releaseJson("v1.2.0-beta1", prerelease=True), releaseJson("v1.1.0")]
		urlopen = FakeUrlopen({updater.RELEASES_URL: json.dumps(data).encode()})
		self.assertEqual(updater.checkForUpdate("1.2.0-beta1", urlopen).version, "1.2.0-beta2")
		self.assertEqual([r.full_url for r, _t in urlopen.requests], [updater.RELEASES_URL])

	def test_shouldCheckNow(self):
		now = 1_800_000_000.0
		self.assertTrue(updater.shouldCheckNow(0, now))
		self.assertTrue(updater.shouldCheckNow(None, now))
		self.assertTrue(updater.shouldCheckNow("bad", now))
		self.assertFalse(updater.shouldCheckNow(now - 3600, now))
		self.assertFalse(updater.shouldCheckNow(now - 24 * 3600 + 1, now))
		self.assertTrue(updater.shouldCheckNow(now - 24 * 3600, now))
		self.assertFalse(updater.shouldCheckNow(now + 3600, now), "slightly ahead: wait")
		self.assertTrue(updater.shouldCheckNow(now + 3 * 24 * 3600, now), "clock was set back")


class TestChecksumsAndDownload(unittest.TestCase):
	def setUp(self):
		self.dir = tempfile.mkdtemp(prefix="askai-update-test-")
		self.addCleanup(shutil.rmtree, self.dir, True)
		self.release = updater.parseRelease(releaseJson("v1.3.0"))
		patcher = mock.patch.object(updater, "SIGNING_PUBLIC_KEY", TEST_PUBLIC)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_parseChecksums(self):
		other = "b" * 64
		text = "# comment\n\n%s  askAI-1.3.0.nvda-addon\n%s *TESTING.txt\nnot-a-hash  x\n%s  short\n" % (ADDON_SHA.upper(), other, "a" * 10)
		self.assertEqual(updater.parseChecksums(text), {"askAI-1.3.0.nvda-addon": ADDON_SHA, "TESTING.txt": other})

	def test_success(self):
		urlopen = FakeUrlopen(downloadRoutes(self.release))
		path = updater.downloadUpdate(self.release, self.dir, urlopen)
		self.assertEqual(path, os.path.join(self.dir, "askAI-1.3.0.nvda-addon"))
		with open(path, "rb") as f:
			self.assertEqual(f.read(), ADDON_BYTES)
		self.assertTrue(all(timeout == 10 for _r, timeout in urlopen.requests))

	def test_mismatchRemovesFile(self):
		checksums = "%s  %s\n" % (ADDON_SHA, self.release.addonAsset.name)
		urlopen = FakeUrlopen(downloadRoutes(self.release, checksums=checksums, addon=ADDON_BYTES + b"tampered"))
		with self.assertRaises(UpdateError) as ctx:
			updater.downloadUpdate(self.release, self.dir, urlopen)
		self.assertEqual(ctx.exception.kind, "checksum")
		self.assertEqual(os.listdir(self.dir), [])

	def test_signatureIsRequiredAndChecked(self):
		release = self.release
		release.signatureAsset = None
		urlopen = FakeUrlopen({})
		with self.assertRaises(UpdateError) as ctx:
			updater.downloadUpdate(release, self.dir, urlopen)
		self.assertEqual(ctx.exception.kind, "signature")
		self.assertEqual(urlopen.requests, [], "nothing is downloaded without a signature file")

	def test_badSignaturesAreRefused(self):
		routes = downloadRoutes(self.release)
		good = routes[self.release.signatureAsset.url]
		otherKey = bytes([7] * 32)
		cases = {
			"wrong key": downloadRoutes(self.release, secret=otherKey),
			"flipped bit": dict(routes, **{self.release.signatureAsset.url: bytes([good[0] ^ 1]) + good[1:]}),
			"empty": dict(routes, **{self.release.signatureAsset.url: b""}),
			"garbage text": dict(routes, **{self.release.signatureAsset.url: b"not a signature"}),
		}
		# Checksums changed after signing (an attacker swapping the add-on and its hash).
		tampered = dict(routes)
		tampered[self.release.checksumsAsset.url] = ("%s  %s\n" % ("0" * 64, self.release.addonAsset.name)).encode()
		cases["checksums changed after signing"] = tampered
		for name, caseRoutes in cases.items():
			with self.subTest(case=name):
				urlopen = FakeUrlopen(caseRoutes)
				with self.assertRaises(UpdateError) as ctx:
					updater.downloadUpdate(self.release, self.dir, urlopen)
				self.assertEqual(ctx.exception.kind, "signature")
				self.assertNotIn(self.release.addonAsset.url, [r.full_url for r, _t in urlopen.requests], "add-on not fetched")
				self.assertEqual(os.listdir(self.dir), [])
		# The real release key is not the test key: a test-signed release is refused by the shipped key.
		self.assertFalse(
			updater.verifyChecksumsSignature(b"x", ed25519.sign(TEST_SECRET, b"x"), publicKey=bytes.fromhex("aa00000b0fc84e10e2b6071c2eae9c1f2c709160c7f478d8af02deafe7a9e6ce")),
		)

	def test_base64Signature(self):
		import base64

		routes = downloadRoutes(self.release)
		routes[self.release.signatureAsset.url] = base64.b64encode(routes[self.release.signatureAsset.url]) + b"\n"
		path = updater.downloadUpdate(self.release, self.dir, FakeUrlopen(routes))
		self.assertTrue(os.path.isfile(path))
		self.assertIsNone(updater.parseSignature(b"QUJD"))
		self.assertIsNone(updater.parseSignature(b"x" * 65))

	def test_signatureFileSizeCap(self):
		routes = downloadRoutes(self.release)
		routes[self.release.signatureAsset.url] = b"A" * (updater.MAX_SIGNATURE_BYTES + 1)
		with self.assertRaises(UpdateError) as ctx:
			updater.downloadUpdate(self.release, self.dir, FakeUrlopen(routes))
		self.assertEqual(ctx.exception.kind, "tooLarge")

	def test_urlsArePinnedToTheReleaseTag(self):
		base = DL + "/v1.3.0/"
		self.assertTrue(updater.isPinnedAssetUrl(base + "askAI-1.3.0.nvda-addon", "v1.3.0"))
		for url, tag in (
			(DL + "/v1.2.9/askAI-1.3.0.nvda-addon", "v1.3.0"),
			("https://github.com/attacker/nvda-ask-gpt/releases/download/v1.3.0/askAI-1.3.0.nvda-addon", "v1.3.0"),
			("https://github.com/shea786/nvda-ask-gpt-evil/releases/download/v1.3.0/a.nvda-addon", "v1.3.0"),
			(base + "sub/askAI-1.3.0.nvda-addon", "v1.3.0"),
			(base + "askAI-1.3.0.nvda-addon?x=1", "v1.3.0"),
			(base, "v1.3.0"),
			("http://github.com/shea786/nvda-ask-gpt/releases/download/v1.3.0/a.nvda-addon", "v1.3.0"),
			("https://objects.githubusercontent.com/github-production-release-asset/x", "v1.3.0"),
			(base + "a.nvda-addon", ""),
		):
			self.assertFalse(updater.isPinnedAssetUrl(url, tag), url)
		self.assertEqual(updater.releaseDownloadPrefix("v1.3.0-beta 1"), DL + "/v1.3.0-beta%201/")
		for attr in ("addonAsset", "checksumsAsset", "signatureAsset"):
			with self.subTest(asset=attr):
				release = updater.parseRelease(releaseJson("v1.3.0"))
				getattr(release, attr).url = DL + "/v1.2.0/" + getattr(release, attr).name
				urlopen = FakeUrlopen(downloadRoutes(release))
				with self.assertRaises(UpdateError) as ctx:
					updater.downloadUpdate(release, self.dir, urlopen)
				self.assertEqual(ctx.exception.kind, "invalid")
				self.assertEqual(urlopen.requests, [])

	def test_manifestMustMatch(self):
		cases = {
			"wrong name": makeAddon('name = otherAddon\nversion = 1.3.0\n'),
			"wrong version": makeAddon('name = askAI\nversion = 1.2.9\n'),
			"missing manifest": makeAddon(None),
			"manifest in a folder": makeAddon(None, extra=[("addon/manifest.ini", "name = askAI\nversion = 1.3.0\n")]),
			"name hidden in a triple-quoted description": makeAddon(
				'name = evil\ndescription = """Nice.\nname = askAI\nversion = 1.3.0\n"""\nversion = 1.3.0\n',
			),
			"not a zip": b"PK\x03\x04 not really a zip" * 10,
		}
		for name, addon in cases.items():
			with self.subTest(case=name):
				with self.assertRaises(UpdateError) as ctx:
					updater.downloadUpdate(self.release, self.dir, FakeUrlopen(downloadRoutes(self.release, addon=addon)))
				self.assertEqual(ctx.exception.kind, "invalid")
				self.assertEqual(os.listdir(self.dir), [], "the refused file is deleted")
		good = makeAddon('name = "askAI"\ndescription = """Line one.\nname = evil\n"""\nversion = "1.3.0"  \n')
		path = updater.downloadUpdate(self.release, self.dir, FakeUrlopen(downloadRoutes(self.release, addon=good)))
		self.assertTrue(os.path.isfile(path))

	def test_parseManifest(self):
		text = (
			"# comment\nname = askAI\nsummary = \"Ask AI\"\n"
			"description = \"\"\"First line\nname = evil\nlast\"\"\"\n"
			"author = 'Asif Ali'\nversion = 1.2.0 # trailing comment\n[section]\n"
		)
		fields = updater.parseManifest(text)
		self.assertEqual(fields["name"], "askAI")
		self.assertEqual(fields["summary"], "Ask AI")
		self.assertEqual(fields["description"], "First line\nname = evil\nlast")
		self.assertEqual(fields["author"], "Asif Ali")
		self.assertEqual(fields["version"], "1.2.0")
		with self.assertRaises(ValueError):
			updater.parseManifest('description = """never closed\nname = askAI\n')
		with self.assertRaises(ValueError):
			updater.parseManifest("name = askAI\nname = evil\n")
		with open(os.path.join(helpers.ROOT, "addon", "manifest.ini"), encoding="utf-8") as f:
			real = updater.parseManifest(f.read())
		self.assertEqual(real["name"], "askAI")
		self.assertIn("ChatGPT", real["description"])

	def test_downloadDeadline(self):
		urlopen = FakeUrlopen(downloadRoutes(self.release))
		with self.assertRaises(UpdateError) as ctx:
			updater.downloadUpdate(self.release, self.dir, urlopen, now=lambda: time.monotonic() - updater.DOWNLOAD_DEADLINE - 1)
		self.assertEqual(ctx.exception.kind, "network")
		self.assertIn("timed out", ctx.exception.message)
		self.assertEqual(updater.DOWNLOAD_DEADLINE, 120)
		path = os.path.join(self.dir, "x.nvda-addon")
		url = self.release.addonAsset.url
		with self.assertRaises(UpdateError):
			updater.downloadToFile(url, path, urlopen=FakeUrlopen({url: ADDON_BYTES}), deadline=time.monotonic() - 1)
		self.assertFalse(os.path.exists(path))

	def test_tempDirs(self):
		folder = tempfile.mkdtemp(prefix="askai-tmp-root-")
		self.addCleanup(shutil.rmtree, folder, True)
		with mock.patch.object(updater.tempfile, "gettempdir", lambda: folder), mock.patch.object(updater.tempfile, "tempdir", folder):
			made = updater.makeTempDir()
			self.assertTrue(os.path.basename(made).startswith("askAI-update-"))
			other = os.path.join(folder, "keep-me")
			os.makedirs(other)
			updater.removeTempDir(other)
			self.assertTrue(os.path.isdir(other), "only askAI-update-* folders are ever removed")
			updater.removeTempDir(made)
			self.assertFalse(os.path.exists(made))
			os.makedirs(os.path.join(folder, "askAI-update-old1"))
			os.makedirs(os.path.join(folder, "askAI-update-old2"))
			self.assertEqual(updater.cleanStaleTempDirs(folder), 2)
			self.assertEqual(os.listdir(folder), ["keep-me"])

	def test_missingEntry(self):
		urlopen = FakeUrlopen(downloadRoutes(self.release, checksums="%s  other.nvda-addon\n" % ADDON_SHA))
		with self.assertRaises(UpdateError) as ctx:
			updater.downloadUpdate(self.release, self.dir, urlopen)
		self.assertEqual(ctx.exception.kind, "checksum")
		self.assertEqual(os.listdir(self.dir), [])

	def test_missingChecksumsAsset(self):
		self.release.checksumsAsset = None
		urlopen = FakeUrlopen({})
		with self.assertRaises(UpdateError) as ctx:
			updater.downloadUpdate(self.release, self.dir, urlopen)
		self.assertEqual(ctx.exception.kind, "checksum")
		self.assertIn("SHA256SUMS.txt", ctx.exception.message)
		self.assertEqual(urlopen.requests, [], "nothing is downloaded without a checksum file")

	def test_disallowedAssetUrl(self):
		self.release.addonAsset.url = "https://evil.example/askAI-1.3.0.nvda-addon"
		with self.assertRaises(UpdateError) as ctx:
			updater.downloadUpdate(self.release, self.dir, FakeUrlopen(downloadRoutes(self.release)))
		self.assertEqual(ctx.exception.kind, "invalid")

	def test_unsafeAssetName(self):
		self.release.addonAsset.name = "..\\..\\evil.nvda-addon"
		self.assertEqual(updater.safeAssetName(self.release.addonAsset.name), "evil.nvda-addon")
		for name in ("evil.exe", "", ".nvda-addon", "a b.nvda-addon"):
			self.assertIsNone(updater.safeAssetName(name), name)

	def test_tooLargeByContentLength(self):
		routes = downloadRoutes(self.release)
		url = self.release.addonAsset.url
		routes[url] = lambda request: FakeResponse(ADDON_BYTES, request.full_url, contentLength=updater.MAX_DOWNLOAD_BYTES + 1)
		with self.assertRaises(UpdateError) as ctx:
			updater.downloadUpdate(self.release, self.dir, FakeUrlopen(routes))
		self.assertEqual(ctx.exception.kind, "tooLarge")
		self.assertEqual(os.listdir(self.dir), [])

	def test_tooLargeWhileStreaming(self):
		path = os.path.join(self.dir, "big.nvda-addon")
		url = self.release.addonAsset.url
		urlopen = FakeUrlopen({url: lambda request: FakeResponse(b"x" * 5000, request.full_url, contentLength=None)})
		with self.assertRaises(UpdateError) as ctx:
			updater.downloadToFile(url, path, maxBytes=1000, urlopen=urlopen)
		self.assertEqual(ctx.exception.kind, "tooLarge")
		self.assertFalse(os.path.exists(path))

	def test_downloadToFileDigest(self):
		path = os.path.join(self.dir, "a.nvda-addon")
		url = self.release.addonAsset.url
		self.assertEqual(updater.downloadToFile(url, path, urlopen=FakeUrlopen({url: ADDON_BYTES})), ADDON_SHA)


class TestReleaseKeyAndTools(unittest.TestCase):
	def test_embeddedPublicKeyIsAValidEd25519Key(self):
		self.assertEqual(len(updater.SIGNING_PUBLIC_KEY), 32)
		self.assertIsNotNone(ed25519._decompress(updater.SIGNING_PUBLIC_KEY))
		self.assertNotEqual(updater.SIGNING_PUBLIC_KEY, TEST_PUBLIC)

	def test_verifyToolRejectsUnsignedSums(self):
		import subprocess

		folder = tempfile.mkdtemp(prefix="askai-sigtool-")
		self.addCleanup(shutil.rmtree, folder, True)
		sums = os.path.join(folder, "SHA256SUMS.txt")
		sig = os.path.join(folder, "SHA256SUMS.txt.sig")
		with open(sums, "wb") as f:
			f.write(b"%s  askAI-1.2.0.nvda-addon\n" % (b"0" * 64))
		with open(sig, "wb") as f:
			f.write(ed25519.sign(TEST_SECRET, open(sums, "rb").read()))
		tool = os.path.join(helpers.ROOT, "tools", "release", "verify_sums.py")
		result = subprocess.run([sys.executable, "-I", tool, sums, sig], capture_output=True, text=True)
		self.assertEqual(result.returncode, 1, "signed with a key that is not the release key")
		self.assertIn("FAIL", result.stdout)
		with open(sig, "wb") as f:
			f.write(b"short")
		result = subprocess.run([sys.executable, "-I", tool, sums, sig], capture_output=True, text=True)
		self.assertEqual(result.returncode, 1)


class TestUpdateDialogSource(unittest.TestCase):
	def test_buttonsAndMnemonics(self):
		import re

		with open(os.path.join(_PACKAGE, "updateCheck.py"), encoding="utf-8") as f:
			source = f.read()
		start = source.index("class UpdateDialog(")
		body = source[start:source.index("\ndef askAboutUpdate")]
		labels = re.findall(r'label=_\("([^"]*)"\)', body)
		self.assertEqual(labels, ["&Install", "&What's new", "&Skip this version", "&Later"])
		letters = [label[label.index("&") + 1].lower() for label in labels]
		self.assertEqual(len(letters), len(set(letters)))
		self.assertIn("self.SetEscapeId(wx.ID_CANCEL)", body, "Escape means Later")
		self.assertIn("self.installButton.SetDefault()", body)
		self.assertIn("ui.browseableMessage(notes", body)
		self.assertNotIn("askYesNo", source)
		self.assertIn("gui.mainFrame.prePopup()", source)
		# L4: no startfile fallback once NVDA's installer was reached; temp folders are removed.
		install = source[source.index("def installDownloaded"):]
		self.assertLess(install.index("install(path)"), install.index("os.startfile(path)"))
		self.assertIn("return False\n\ttry:\n\t\tos.startfile(path)", install)
		self.assertIn("updater.removeTempDir(folder)", source)
		self.assertIn("updater.cleanStaleTempDirs()", source)
		self.assertIn('"skippedUpdateVersion"', source)


class TestModules(unittest.TestCase):
	def test_updaterIsPure(self):
		with open(os.path.join(_PACKAGE, "updater.py"), encoding="utf-8") as f:
			source = f.read()
		for nvdaModule in ("import wx", "import gui", "import ui", "import addonHandler", "import config", "import globalVars"):
			self.assertNotIn(nvdaModule, source)

	def test_updateCheckCompiles(self):
		tempDir = tempfile.mkdtemp()
		self.addCleanup(shutil.rmtree, tempDir, True)
		py_compile.compile(os.path.join(_PACKAGE, "updateCheck.py"), cfile=os.path.join(tempDir, "u.pyc"), doraise=True)


if __name__ == "__main__":
	unittest.main()
