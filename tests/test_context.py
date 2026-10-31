"""Tests for context.py ("Ask about this") with fake NVDA objects, png.py and screenshot.py declarations."""

import ctypes
import struct
import types
import unittest
import zlib

import helpers  # noqa: F401  (adds the add-on package to sys.path)

import context
import png
import screenshot

PASSWORD_ROLE = "passwordEdit"
PROTECTED_STATE = "protected"


def fake(name="", value="", selection="", text="", isProtected=False, role="editableText", states=()):
	return types.SimpleNamespace(
		name=name,
		value=value,
		selection=selection,
		text=text,
		isProtected=isProtected,
		role=role,
		states=set(states),
	)


def capture(focus, navigator, **kwargs):
	return context.captureContext(
		focus,
		navigator,
		lambda obj: obj.selection,
		lambda obj: obj.text,
		appName=kwargs.pop("appName", "Microsoft Word"),
		windowTitle=kwargs.pop("windowTitle", "Report.docx - Word"),
		isProtected=lambda obj: context.isProtectedObject(obj, PASSWORD_ROLE, PROTECTED_STATE),
		**kwargs,
	)


class TestCapture(unittest.TestCase):
	def test_selectionWins(self):
		focus = fake(selection="Selected words", text="whole document")
		result = capture(focus, fake(name="Nav", text="other"))
		self.assertEqual((result.text, result.source), ("Selected words", "selection"))
		self.assertEqual(result.summary(), "14 characters from Microsoft Word, Report.docx - Word")
		self.assertEqual(result.label(), "Text from the user's screen (Microsoft Word, Report.docx - Word)")
		self.assertNotIn("Selected", result.summary(), "the summary never contains the text")

	def test_navigatorNameValueAndText(self):
		navigator = fake(name="Total", value="42", text="Total 42 euros")
		result = capture(fake(), navigator)
		self.assertEqual(result.source, "navigator")
		self.assertEqual(result.text, "Total 42 euros", "parts already contained in the text are not repeated")
		result = capture(fake(), fake(name="OK", value="", text=""))
		self.assertEqual(result.text, "OK")
		result = capture(fake(), fake(name="Volume", value="50%", text="slider"))
		self.assertEqual(result.text, "Volume\n50%\nslider")

	def test_protectedFieldsAreRefused(self):
		for focus, navigator in (
			(fake(selection="secret", isProtected=True), fake(text="x")),
			(fake(selection="x"), fake(text="secret", role=PASSWORD_ROLE)),
			(fake(selection="x", states=[PROTECTED_STATE]), fake()),
		):
			with self.assertRaises(context.ContextError) as cm:
				capture(focus, navigator)
			self.assertEqual(cm.exception.reason, context.REASON_PROTECTED)

	def test_errorsAndEmpty(self):
		class Broken:
			@property
			def isProtected(self):
				raise RuntimeError("dead object")

		self.assertTrue(context.isProtectedObject(Broken()), "an object that cannot be checked counts as protected")
		self.assertFalse(context.isProtectedObject(None))
		with self.assertRaises(context.ContextError) as cm:
			capture(fake(), fake())
		self.assertEqual(cm.exception.reason, context.REASON_EMPTY)

		def failing(obj):
			raise RuntimeError("no TextInfo")

		result = context.captureContext(fake(), fake(name="Name"), failing, failing)
		self.assertEqual(result.text, "Name")

	def test_capAt20000Characters(self):
		result = capture(fake(selection="x" * 25000), fake())
		self.assertEqual(len(result.text), 20000)
		self.assertTrue(result.truncated)
		self.assertEqual(result.summary(), "20,000 characters from Microsoft Word, Report.docx - Word")
		self.assertEqual(capture(fake(selection="x"), fake(), appName="", windowTitle="").summary(), "1 characters from the screen")


def readPng(data):
	"""(width, height, colour type, raw scanlines) from a PNG, checking every chunk's CRC."""
	assert data[:8] == png.SIGNATURE
	position = 8
	chunks = []
	while position < len(data):
		length = struct.unpack(">I", data[position:position + 4])[0]
		kind = data[position + 4:position + 8]
		body = data[position + 8:position + 8 + length]
		crc = struct.unpack(">I", data[position + 8 + length:position + 12 + length])[0]
		assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF, kind
		chunks.append((kind, body))
		position += 12 + length
	assert [kind for kind, body in chunks] == [b"IHDR", b"IDAT", b"IEND"]
	width, height, depth, colour, compression, filtering, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
	assert (depth, compression, filtering, interlace) == (8, 0, 0, 0)
	return width, height, colour, zlib.decompress(chunks[1][1])


class TestPng(unittest.TestCase):
	def test_rgb(self):
		pixels = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])  # 2 x 2
		width, height, colour, raw = readPng(png.encodeRgb(2, 2, pixels))
		self.assertEqual((width, height, colour), (2, 2, 2))
		self.assertEqual(raw, b"\x00" + pixels[:6] + b"\x00" + pixels[6:])

	def test_bgraTopDownAndBottomUp(self):
		# Two rows of one pixel: blue then red, as BGRA.
		bgra = bytes([255, 0, 0, 255, 0, 0, 255, 255])
		self.assertEqual(readPng(png.encodeBgra(1, 2, bgra))[3], b"\x00\x00\x00\xff\x00\xff\x00\x00")
		self.assertEqual(readPng(png.encodeBgra(1, 2, bgra, topDown=False))[3], b"\x00\xff\x00\x00\x00\x00\x00\xff")

	def test_errors(self):
		with self.assertRaises(ValueError):
			png.encodeRgb(0, 1, b"")
		with self.assertRaises(ValueError):
			png.encodeRgb(2, 2, b"\x00" * 5)
		with self.assertRaises(ValueError):
			png.encodeBgra(2, 2, b"\x00" * 15)

	def test_largeImageCompresses(self):
		data = png.encodeBgra(640, 480, b"\x10\x20\x30\xff" * (640 * 480))
		self.assertLess(len(data), 20000)
		self.assertEqual(readPng(data)[:2], (640, 480))


class TestScreenshotDeclarations(unittest.TestCase):
	def test_structures(self):
		self.assertEqual(ctypes.sizeof(screenshot.BITMAPINFOHEADER), 40)
		self.assertEqual(ctypes.sizeof(screenshot.RECT), 16)
		self.assertEqual(ctypes.sizeof(screenshot.BITMAPINFO), 44)

	def test_importsWithoutWindowsAndLoadsLazily(self):
		self.assertIsNone(screenshot._dlls)

	def test_captureWithFakeGdi(self):
		calls = []

		class Fn:
			def __init__(self, name, result):
				self.name, self.result = name, result

			def __call__(self, *args):
				calls.append(self.name)
				if self.name == "GetWindowRect":
					rect = args[1]._obj
					rect.left, rect.top, rect.right, rect.bottom = 10, 20, 12, 21
				if self.name == "GetDIBits":
					header = args[5]._obj.bmiHeader
					assert header.biHeight == -1 and header.biBitCount == 32 and header.biWidth == 2
					ctypes.memmove(args[4], b"\x01\x02\x03\xff\x04\x05\x06\xff", 8)
				return self.result

		user32 = types.SimpleNamespace(
			GetForegroundWindow=Fn("GetForegroundWindow", 99),
			GetWindowRect=Fn("GetWindowRect", 1),
			GetDC=Fn("GetDC", 5),
			ReleaseDC=Fn("ReleaseDC", 1),
		)
		gdi32 = types.SimpleNamespace(
			CreateCompatibleDC=Fn("CreateCompatibleDC", 6),
			CreateCompatibleBitmap=Fn("CreateCompatibleBitmap", 7),
			SelectObject=Fn("SelectObject", 8),
			BitBlt=Fn("BitBlt", 1),
			GetDIBits=Fn("GetDIBits", 1),
			DeleteObject=Fn("DeleteObject", 1),
			DeleteDC=Fn("DeleteDC", 1),
		)
		width, height, data = screenshot.captureWindow(dlls=(user32, gdi32))
		self.assertEqual((width, height, data), (2, 1, b"\x01\x02\x03\xff\x04\x05\x06\xff"))
		for name in ("DeleteObject", "DeleteDC", "ReleaseDC"):
			self.assertIn(name, calls)


if __name__ == "__main__":
	unittest.main()
