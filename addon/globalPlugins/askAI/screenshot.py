# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: capture the foreground window as 32-bit BGRA pixels with GDI (ctypes), for
# "Ask about this" with "Include a screenshot of the window". png.py turns the pixels into a PNG.
# Private WinDLL instances with full argtypes/restype (handles are HANDLE = c_void_p), loaded only on
# Windows and only when a screenshot is taken, so 32-bit and 64-bit NVDA both get correct pointers.
# No NVDA imports.

import ctypes

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
BI_RGB = 0
DIB_RGB_COLORS = 0
# Larger windows are refused (a 4K screen is 3840 x 2160).
MAX_SIDE = 8192


class RECT(ctypes.Structure):
	_fields_ = [
		("left", ctypes.c_int32),  # LONG is 32 bits on Windows (c_long would be 64 bits elsewhere)
		("top", ctypes.c_int32),
		("right", ctypes.c_int32),
		("bottom", ctypes.c_int32),
	]


class BITMAPINFOHEADER(ctypes.Structure):
	_fields_ = [
		("biSize", ctypes.c_uint32),  # DWORD
		("biWidth", ctypes.c_int32),  # LONG
		("biHeight", ctypes.c_int32),  # LONG; negative means top-down rows
		("biPlanes", ctypes.c_uint16),  # WORD
		("biBitCount", ctypes.c_uint16),  # WORD
		("biCompression", ctypes.c_uint32),  # DWORD
		("biSizeImage", ctypes.c_uint32),  # DWORD
		("biXPelsPerMeter", ctypes.c_int32),  # LONG
		("biYPelsPerMeter", ctypes.c_int32),  # LONG
		("biClrUsed", ctypes.c_uint32),  # DWORD
		("biClrImportant", ctypes.c_uint32),  # DWORD
	]


class BITMAPINFO(ctypes.Structure):
	_fields_ = [
		("bmiHeader", BITMAPINFOHEADER),
		("bmiColors", ctypes.c_uint32 * 1),  # RGBQUAD[1]
	]


_dlls = None


def _load():
	"""(user32, gdi32) with prototypes; private instances, never ctypes.windll's shared ones."""
	global _dlls
	if _dlls is None:
		from ctypes import wintypes

		user32 = ctypes.WinDLL("user32", use_last_error=True)
		gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
		user32.GetForegroundWindow.argtypes = ()
		user32.GetForegroundWindow.restype = wintypes.HWND
		user32.IsWindow.argtypes = (wintypes.HWND,)
		user32.IsWindow.restype = wintypes.BOOL
		user32.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
		user32.PrintWindow.restype = wintypes.BOOL
		user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(RECT))
		user32.GetWindowRect.restype = wintypes.BOOL
		user32.GetDC.argtypes = (wintypes.HWND,)
		user32.GetDC.restype = wintypes.HDC
		user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
		user32.ReleaseDC.restype = ctypes.c_int
		gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
		gdi32.CreateCompatibleDC.restype = wintypes.HDC
		gdi32.CreateCompatibleBitmap.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
		gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
		gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
		gdi32.SelectObject.restype = wintypes.HGDIOBJ
		gdi32.BitBlt.argtypes = (
			wintypes.HDC,
			ctypes.c_int,
			ctypes.c_int,
			ctypes.c_int,
			ctypes.c_int,
			wintypes.HDC,
			ctypes.c_int,
			ctypes.c_int,
			wintypes.DWORD,
		)
		gdi32.BitBlt.restype = wintypes.BOOL
		gdi32.GetDIBits.argtypes = (
			wintypes.HDC,
			wintypes.HBITMAP,
			wintypes.UINT,
			wintypes.UINT,
			ctypes.c_void_p,
			ctypes.POINTER(BITMAPINFO),
			wintypes.UINT,
		)
		gdi32.GetDIBits.restype = ctypes.c_int
		gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
		gdi32.DeleteObject.restype = wintypes.BOOL
		gdi32.DeleteDC.argtypes = (wintypes.HDC,)
		gdi32.DeleteDC.restype = wintypes.BOOL
		_dlls = (user32, gdi32)
	return _dlls


def windowExists(hwnd, dlls=None):
	if not hwnd:
		return False
	user32, _gdi32 = dlls or _load()
	return bool(user32.IsWindow(hwnd))


def captureWindow(hwnd=None, dlls=None):
	"""(width, height, BGRA bytes with top-down rows) of the window, or None on failure.
	PrintWindow(PW_RENDERFULLCONTENT) asks the window to draw itself, so other windows covering it (such as
	the Ask AI dialog) are not in the picture; if that fails the window's area is copied from the screen.
	"""
	user32, gdi32 = dlls or _load()
	if not hwnd:
		hwnd = user32.GetForegroundWindow()
	if not hwnd or not user32.IsWindow(hwnd):
		return None
	rect = RECT()
	if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
		return None
	width, height = rect.right - rect.left, rect.bottom - rect.top
	if width <= 0 or height <= 0 or width > MAX_SIDE or height > MAX_SIDE:
		return None
	screenDc = user32.GetDC(None)
	if not screenDc:
		return None
	memoryDc = bitmap = old = None
	try:
		memoryDc = gdi32.CreateCompatibleDC(screenDc)
		bitmap = gdi32.CreateCompatibleBitmap(screenDc, width, height)
		if not memoryDc or not bitmap:
			return None
		old = gdi32.SelectObject(memoryDc, bitmap)
		if not user32.PrintWindow(hwnd, memoryDc, PW_RENDERFULLCONTENT):
			if not gdi32.BitBlt(memoryDc, 0, 0, width, height, screenDc, rect.left, rect.top, SRCCOPY | CAPTUREBLT):
				return None
		info = BITMAPINFO()
		info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
		info.bmiHeader.biWidth = width
		info.bmiHeader.biHeight = -height  # Top-down rows.
		info.bmiHeader.biPlanes = 1
		info.bmiHeader.biBitCount = 32
		info.bmiHeader.biCompression = BI_RGB
		buffer = ctypes.create_string_buffer(width * height * 4)
		# The bitmap must not be selected into a DC while GetDIBits reads it.
		gdi32.SelectObject(memoryDc, old)
		old = None
		lines = gdi32.GetDIBits(memoryDc, bitmap, 0, height, buffer, ctypes.byref(info), DIB_RGB_COLORS)
		if lines != height:
			return None
		return width, height, buffer.raw
	finally:
		if old:
			gdi32.SelectObject(memoryDc, old)
		if bitmap:
			gdi32.DeleteObject(bitmap)
		if memoryDc:
			gdi32.DeleteDC(memoryDc)
		user32.ReleaseDC(None, screenDc)
