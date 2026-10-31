# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: a minimal PNG encoder (8-bit RGB, no interlace) using only zlib and struct,
# for window screenshots. Pure standard library, no NVDA imports.

import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind, data):
	return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def rgbRowsFromBgra(width, height, data, topDown=True):
	"""PNG scanlines (filter byte 0 + RGB) from 32-bit BGRA pixels, as GetDIBits returns them.
	@param topDown: False when the first row in data is the bottom row of the image.
	"""
	stride = width * 4
	if len(data) < stride * height:
		raise ValueError("not enough pixel data: %d < %d" % (len(data), stride * height))
	rows = []
	order = range(height) if topDown else range(height - 1, -1, -1)
	for y in order:
		row = data[y * stride:(y + 1) * stride]
		rgb = bytearray(width * 3)
		rgb[0::3] = row[2::4]
		rgb[1::3] = row[1::4]
		rgb[2::3] = row[0::4]
		rows.append(b"\x00" + bytes(rgb))
	return b"".join(rows)


def encodeRgb(width, height, rgb, level=6):
	"""PNG bytes for width x height 8-bit RGB pixels (rgb is width*height*3 bytes, rows top to bottom)."""
	if width <= 0 or height <= 0:
		raise ValueError("empty image")
	stride = width * 3
	if len(rgb) != stride * height:
		raise ValueError("pixel data has the wrong size")
	raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
	return _encodeScanlines(width, height, raw, level)


def encodeBgra(width, height, data, topDown=True, level=6):
	"""PNG bytes from 32-bit BGRA pixels (alpha ignored)."""
	if width <= 0 or height <= 0:
		raise ValueError("empty image")
	return _encodeScanlines(width, height, rgbRowsFromBgra(width, height, data, topDown), level)


def _encodeScanlines(width, height, raw, level):
	header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit depth, colour type 2 (RGB).
	return SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw, level)) + _chunk(b"IEND", b"")


MAX_SIDE = 4096


def downscaleBgra(width, height, data, maxSide=MAX_SIDE, topDown=True):
	"""(width, height, data) no larger than maxSide pixels per side, by keeping every n-th pixel and row.
	Returns the input unchanged when it already fits.
	"""
	factor = max(1, -(-width // maxSide), -(-height // maxSide))
	if factor == 1:
		return width, height, data
	stride = width * 4
	pixels = memoryview(data)
	rows = []
	newWidth = len(range(0, width, factor))
	for y in range(0, height, factor):
		row = pixels[y * stride:(y + 1) * stride].cast("I")
		rows.append(row[::factor].tobytes())
	return newWidth, len(rows), b"".join(rows)


def isNearlyUniform(width, height, data, share=0.98, samples=20000):
	"""True when almost every sampled pixel has the same colour (a blank or curtained window)."""
	count = width * height
	if count <= 0 or len(data) < count * 4:
		return True
	pixels = memoryview(data)[:count * 4].cast("I")
	step = max(1, count // samples)
	counts = {}
	total = 0
	for value in pixels[::step]:
		value &= 0x00FFFFFF  # Ignore alpha.
		counts[value] = counts.get(value, 0) + 1
		total += 1
	return max(counts.values()) >= share * total
