"""Safely open workspace directory entries."""

import os
import stat
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _open_windows = _kernel.CreateFileW
    _open_windows.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    _open_windows.restype = wintypes.HANDLE
    _close_windows = _kernel.CloseHandle
    _close_windows.argtypes = (wintypes.HANDLE,)
    _close_windows.restype = wintypes.BOOL

    @contextmanager
    def _windows_entry(path: Path):
        # Open the entry itself and keep it pinned against writes and replacement
        # while inspecting it or reading a child by path.
        read_attributes, share_read, open_existing = 0x80, 1, 3
        open_reparse_point, backup_semantics = 0x00200000, 0x02000000
        handle = _open_windows(
            str(path), read_attributes, share_read, None, open_existing,
            open_reparse_point | backup_semantics, None,
        )
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            metadata = path.lstat()
            if metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("Skill workspace cannot contain symbolic links or reparse points")
            yield metadata
        finally:
            _close_windows(handle)


@contextmanager
def _directory(parent: int | Path, name: str | None = None):
    if os.name == "nt":
        path = parent if name is None else parent / name
        with _windows_entry(path) as metadata:
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("Skill workspace path is not a directory")
            yield path
        return

    if name is None:
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    else:
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)
