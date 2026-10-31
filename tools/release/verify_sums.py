#!/usr/bin/env python3
"""Verify SHA256SUMS.txt.sig with the add-on's own pure-Python Ed25519 verifier and the public key embedded
in updater.py, exactly as installed copies of Ask AI will. Used by the release workflow after signing.

Usage: python3 -I tools/release/verify_sums.py SHA256SUMS.txt SHA256SUMS.txt.sig
Exit status 0 when the signature is valid, 1 otherwise. Prints no key material.
"""

import base64
import binascii
import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PACKAGE = os.path.join(ROOT, "addon", "globalPlugins", "askAI")


def loadVerifier():
	spec = importlib.util.spec_from_file_location("askAIEd25519", os.path.join(PACKAGE, "ed25519.py"))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def embeddedPublicKey():
	with open(os.path.join(PACKAGE, "updater.py"), encoding="utf-8") as f:
		source = f.read()
	match = re.search(r'^SIGNING_PUBLIC_KEY = bytes\.fromhex\("([0-9a-f]{64})"\)', source, re.MULTILINE)
	if not match:
		raise SystemExit("updater.py has no SIGNING_PUBLIC_KEY")
	return bytes.fromhex(match.group(1))


def parseSignature(data):
	if len(data) == 64:
		return data
	try:
		decoded = base64.b64decode(data.strip(), validate=True)
	except (binascii.Error, ValueError):
		return None
	return decoded if len(decoded) == 64 else None


def main(argv):
	if len(argv) != 3:
		print(__doc__.strip())
		return 2
	with open(argv[1], "rb") as f:
		sums = f.read()
	with open(argv[2], "rb") as f:
		signature = parseSignature(f.read())
	if signature is None:
		print("FAIL: %s is not a 64-byte Ed25519 signature" % argv[2])
		return 1
	if not loadVerifier().verify(embeddedPublicKey(), sums, signature):
		print("FAIL: %s is not signed by the key embedded in updater.py" % argv[1])
		return 1
	print("OK: %s is signed by the key embedded in updater.py" % argv[1])
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv))
