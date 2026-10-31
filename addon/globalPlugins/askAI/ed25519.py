# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: Ed25519 signatures (RFC 8032), used to verify that an update's SHA256SUMS.txt was
# signed with the release key. Pure standard library (hashlib.sha512), no NVDA imports.
# Follows the reference implementation in RFC 8032, section 6. It is not constant-time, which is fine
# for verifying public data; sign() exists only for tests and release tooling.

import hashlib

_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493  # The group order L.


def _sha512(data):
	return hashlib.sha512(data).digest()


def _inverse(x):
	return pow(x, _P - 2, _P)


_D = -121665 * _inverse(121666) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _sha512ModQ(data):
	return int.from_bytes(_sha512(data), "little") % _Q


def _pointAdd(a, b):
	# Extended homogeneous coordinates (X, Y, Z, T) with x = X/Z, y = Y/Z, x*y = T/Z.
	first = (a[1] - a[0]) * (b[1] - b[0]) % _P
	second = (a[1] + a[0]) * (b[1] + b[0]) % _P
	third = 2 * a[3] * b[3] * _D % _P
	fourth = 2 * a[2] * b[2] % _P
	e, f, g, h = second - first, fourth - third, fourth + third, second + first
	return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _pointMul(scalar, point):
	result = (0, 1, 1, 0)  # The neutral element.
	while scalar > 0:
		if scalar & 1:
			result = _pointAdd(result, point)
		point = _pointAdd(point, point)
		scalar >>= 1
	return result


def _pointEqual(a, b):
	if (a[0] * b[2] - b[0] * a[2]) % _P != 0:
		return False
	return (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recoverX(y, sign):
	if y >= _P:
		return None
	x2 = (y * y - 1) * _inverse(_D * y * y + 1) % _P
	if x2 == 0:
		return None if sign else 0
	x = pow(x2, (_P + 3) // 8, _P)
	if (x * x - x2) % _P != 0:
		x = x * _SQRT_M1 % _P
	if (x * x - x2) % _P != 0:
		return None
	if (x & 1) != sign:
		x = _P - x
	return x


_GY = 4 * _inverse(5) % _P
_GX = _recoverX(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(point):
	zInverse = _inverse(point[2])
	x = point[0] * zInverse % _P
	y = point[1] * zInverse % _P
	return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(data):
	if len(data) != 32:
		return None
	y = int.from_bytes(data, "little")
	sign = y >> 255
	y &= (1 << 255) - 1
	x = _recoverX(y, sign)
	if x is None:
		return None
	return (x, y, 1, x * y % _P)


def _expandSecret(secret):
	if not isinstance(secret, (bytes, bytearray)) or len(secret) != 32:
		raise ValueError("an Ed25519 secret key is 32 bytes")
	digest = _sha512(bytes(secret))
	scalar = int.from_bytes(digest[:32], "little")
	scalar &= (1 << 254) - 8
	scalar |= 1 << 254
	return scalar, digest[32:]


def publicKey(secret):
	"""The 32-byte public key of a 32-byte secret key."""
	scalar, _prefix = _expandSecret(secret)
	return _compress(_pointMul(scalar, _G))


def sign(secret, message):
	"""The 64-byte signature of message (for tests and release tooling; releases are signed with openssl)."""
	scalar, prefix = _expandSecret(secret)
	public = _compress(_pointMul(scalar, _G))
	r = _sha512ModQ(prefix + message)
	encodedR = _compress(_pointMul(r, _G))
	h = _sha512ModQ(encodedR + public + message)
	s = (r + h * scalar) % _Q
	return encodedR + int.to_bytes(s, 32, "little")


def verify(public, message, signature):
	"""True only for a valid signature of message under the 32-byte public key. Never raises for bad input."""
	if not isinstance(public, (bytes, bytearray)) or len(public) != 32:
		return False
	if not isinstance(signature, (bytes, bytearray)) or len(signature) != 64:
		return False
	if not isinstance(message, (bytes, bytearray)):
		return False
	public, signature, message = bytes(public), bytes(signature), bytes(message)
	pointA = _decompress(public)
	if pointA is None:
		return False
	encodedR = signature[:32]
	pointR = _decompress(encodedR)
	if pointR is None:
		return False
	s = int.from_bytes(signature[32:], "little")
	if s >= _Q:
		return False
	h = _sha512ModQ(encodedR + public + message)
	return _pointEqual(_pointMul(s, _G), _pointAdd(pointR, _pointMul(h, pointA)))
