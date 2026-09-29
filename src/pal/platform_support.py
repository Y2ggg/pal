"""Native platform boundaries for files, shells and process inspection.

Traceability: PRD-TECH-001, PRD-P0-002; ACC-001/011/012.
"""

import os
import shlex
import stat
from pathlib import Path


def is_link(path: Path) -> bool:
    """Reject Windows junctions and other reparse points as well as symlinks."""
    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except FileNotFoundError:
            return False
    return False


def shell_quote(value: str) -> str:
    if os.name == "nt":
        return "'" + value.replace("'", "''") + "'"
    return shlex.quote(value)


def move_path(source: Path, destination: Path, *, replace: bool = False) -> None:
    """Same-volume publish, with Windows write-through and POSIX directory sync."""
    if os.name != "nt":
        (os.replace if replace else os.rename)(source, destination)
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    move = kernel.MoveFileExW
    move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move.restype = wintypes.BOOL
    # No COPY_ALLOWED: a cross-volume copy cannot preserve atomic publication.
    if not move(str(source), str(destination), 8 | (1 if replace else 0)):
        raise ctypes.WinError(ctypes.get_last_error())


def process_exists(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        code = ctypes.get_last_error()
        if code == 5:
            return True
        if code == 87:
            return False
        raise ctypes.WinError(code)
    try:
        status = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(status)):
            raise ctypes.WinError(ctypes.get_last_error())
        return status.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def command_for_platform(arguments: list[str]) -> list[str]:
    """Resolve Windows native executables or known npm entrypoints without cmd.exe."""
    if os.name != "nt" or not arguments:
        return arguments
    return windows_command(arguments)


def windows_command(arguments: list[str]) -> list[str]:
    """Resolve a native binary or the bin declared by an official npm package."""
    import json
    import shutil

    from .errors import CompatibilityError

    executable = shutil.which(arguments[0])
    if executable is None:
        return arguments
    if Path(executable).suffix.lower() not in {".cmd", ".bat"}:
        return [executable, *arguments[1:]]
    name = Path(executable).stem.lower()
    packages = {"codex": "@openai/codex", "claude": "@anthropic-ai/claude-code"}
    package = Path(executable).parent / "node_modules" / packages.get(name, "__unsupported__")
    try:
        metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
        if (
            name not in packages
            or not isinstance(metadata, dict)
            or metadata.get("name") != packages[name]
        ):
            raise ValueError("unexpected npm package")
        entries = metadata.get("bin", {})
        relative = entries.get(name) if isinstance(entries, dict) else entries
        if not isinstance(relative, str):
            raise ValueError("npm entry missing")
        entry = (package / relative).resolve(strict=True)
        entry.relative_to(package.resolve(strict=True))
        if entry.is_file() and entry.suffix.lower() == ".exe":
            return [str(entry), *arguments[1:]]
        node = shutil.which("node.exe")
        if entry.is_file() and entry.suffix.lower() in {".js", ".cjs", ".mjs"} and node:
            return [node, str(entry), *arguments[1:]]
    except (OSError, ValueError, TypeError):
        pass
    raise CompatibilityError(
        f"无法安全启动 Windows CLI 包装脚本：{executable}；请使用官方原生安装或标准 npm 安装"
    )


def validate_windows_path(path: Path, *, check_links: bool = True) -> None:
    """Local drive paths only; reject aliases before filesystem normalization."""
    if os.name != "nt":
        return
    from .errors import PathSafetyError

    if path.drive and (len(path.drive) != 2 or path.drive[1] != ":" or not path.is_absolute()):
        raise PathSafetyError("PAL requires a local Windows drive, not UNC/device paths")
    for part in path.parts[1:] if path.anchor else path.parts:
        stem = part.split(".")[0].rstrip(" ").upper()
        if (
            part.endswith((".", " "))
            or any(c in '<>:"|?*' or ord(c) < 32 for c in part)
            or stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
            or stem in {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in "123456789¹²³"}
        ):
            raise PathSafetyError(f"unsupported Windows path component: {part}")
    if not check_links:
        return
    cursor = Path(path.anchor)
    for part in path.parts[1:] if path.anchor else path.parts:
        cursor /= part
        if is_link(cursor):
            raise PathSafetyError(f"path contains a symbolic link or reparse point: {cursor}")


def external_environment(environment=None):
    """Keep frozen PAL libraries out of unrelated vendor executable search paths."""
    import sys

    if not getattr(sys, "frozen", False):
        return environment
    result = dict(os.environ if environment is None else environment)
    if os.name == "nt":
        import ctypes

        if not ctypes.windll.kernel32.SetDllDirectoryW(None):
            raise ctypes.WinError()
    else:
        for key in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
            original = result.pop(key + "_ORIG", None)
            if original is None:
                result.pop(key, None)
            else:
                result[key] = original
    return result


def run_external(arguments, **kwargs):
    import subprocess

    kwargs["env"] = external_environment(kwargs.get("env"))
    return subprocess.run(command_for_platform(arguments), **kwargs)


def call_external(arguments, **kwargs):
    import subprocess

    kwargs["env"] = external_environment(kwargs.get("env"))
    return subprocess.call(command_for_platform(arguments), **kwargs)


def popen_external(arguments, **kwargs):
    import subprocess

    kwargs["env"] = external_environment(kwargs.get("env"))
    return subprocess.Popen(command_for_platform(arguments), **kwargs)
