"""Windows user-scoped protection for saved service credentials."""

import base64
import ctypes
import os
from ctypes import wintypes


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def transform(value, decrypt=False):
    if os.name != "nt" or not value:
        return value
    prefix = "dpapi:"
    if decrypt and not value.startswith(prefix):
        return value
    raw = base64.b64decode(value[len(prefix) :]) if decrypt else value.encode("utf-8")
    buf = ctypes.create_string_buffer(raw)
    src = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    out = Blob()
    api = ctypes.windll.crypt32
    fn = api.CryptUnprotectData if decrypt else api.CryptProtectData
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(out)):
        raise ValueError("无法使用当前 Windows 账户读取或保存凭据")
    try:
        result = ctypes.string_at(out.data, out.size)
        return result.decode("utf-8") if decrypt else prefix + base64.b64encode(result).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(out.data)
