"""Nonblocking shared/exclusive process locks on POSIX and Windows.

Traceability: PRD-P0-002/003, PRD-MOUNT-004; ACC-008/011/012.
Lock the same byte of a separate lock file; no payload file is truncated.
"""

import errno
import os

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8
SUPPORTED = os.name in {"posix", "nt"}

if os.name == "posix":
    from fcntl import flock
elif os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _kernel.LockFileEx.restype = wintypes.BOOL
    _kernel.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _kernel.UnlockFileEx.restype = wintypes.BOOL

    def flock(file, operation):
        descriptor = file if isinstance(file, int) else file.fileno()
        handle = msvcrt.get_osfhandle(descriptor)
        overlap = _Overlapped()
        if operation == LOCK_UN:
            if not _kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlap)):
                code = ctypes.get_last_error()
                if code != 158:  # ERROR_NOT_LOCKED: also safe after failed acquisition.
                    raise ctypes.WinError(code)
            return
        if operation not in {LOCK_SH | LOCK_NB, LOCK_EX | LOCK_NB}:
            raise ValueError("PAL requires nonblocking shared or exclusive locks")
        flags = 1 | (2 if operation & LOCK_EX else 0)  # FAIL_IMMEDIATELY, EXCLUSIVE_LOCK
        if not _kernel.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlap)):
            code = ctypes.get_last_error()
            if code == 33:  # ERROR_LOCK_VIOLATION
                raise BlockingIOError(errno.EAGAIN, "PAL lock is held by another operation")
            raise ctypes.WinError(code)
else:

    def flock(file, operation):
        raise OSError("PAL requires POSIX or Windows file locking")
