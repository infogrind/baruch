"""The NAS-specific parts: finding docked drives, mount check, free space."""

from __future__ import annotations

import glob
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

HOMEBREW_PREFIXES = ("/home/linuxbrew/.linuxbrew", "/opt/homebrew", "/usr/local")


class Host(Protocol):
    def drive_paths(self) -> list[Path]: ...
    def is_mount(self, path: Path) -> bool: ...
    def disk_usage(self, path: Path) -> tuple[int, int]: ...
    def fs_type(self, path: Path) -> str: ...
    def now(self) -> datetime: ...
    def rsync(self) -> str: ...


class RealHost:
    """A Synology NAS: USB drives mount as /volumeUSB*/usbshare*."""

    drive_glob = "/volumeUSB*/usbshare*"

    def drive_paths(self) -> list[Path]:
        return sorted(Path(p) for p in glob.glob(self.drive_glob) if os.path.isdir(p))

    def is_mount(self, path: Path) -> bool:
        return os.path.ismount(path)

    def disk_usage(self, path: Path) -> tuple[int, int]:
        st = os.statvfs(path)
        return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize

    def fs_type(self, path: Path) -> str:
        best, kind = "", "unknown"
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    mount = parts[1].replace("\\040", " ")
                    if str(path).startswith(mount) and len(mount) > len(best):
                        best, kind = mount, parts[2]
        except OSError:
            pass
        return kind

    def now(self) -> datetime:
        return datetime.now(UTC)

    def rsync(self) -> str:
        for prefix in HOMEBREW_PREFIXES:
            candidate = os.path.join(prefix, "opt", "rsync", "bin", "rsync")
            if os.access(candidate, os.X_OK):
                return candidate
        found = shutil.which("rsync")
        if not found:
            raise FileNotFoundError(
                "rsync not found: install it with `brew install rsync`"
            )
        return found
