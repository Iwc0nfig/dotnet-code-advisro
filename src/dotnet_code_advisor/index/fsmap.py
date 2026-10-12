"""Portable source analysis and regression checks."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class CaseMap:
    """Resolve a path whose case may not match disk, caching one listing per directory.

    ``resolve`` returns the real on-disk path, or ``None`` when no file matches
    even case-insensitively.  An exact hit costs one ``stat``; only a miss pays
    for the directory listing, and that listing is then reused.
    """

    def __init__(self) -> None:
        self._dirs: dict[str, dict[str, str]] = {}
        # How often the answer differed from what was written.  Reported through
        # index_status rather than left invisible.
        self.case_resolved = 0

    def _listing(self, directory: Path) -> dict[str, str]:
        key = directory.as_posix()
        entry = self._dirs.get(key)
        if entry is None:
            try:
                entry = {e.name.lower(): e.name for e in os.scandir(directory)}
            except OSError:
                entry = {}
            self._dirs[key] = entry
        return entry

    def resolve(self, candidate: Path) -> Optional[Path]:
        """The real on-disk path for *candidate*, or ``None`` if nothing matches.

        The directory listing is consulted first, deliberately, rather than
        trusting ``exists()``.  On NTFS ``exists()`` is true for a mis-cased path
        and would hand back the case as *written*, which then fails to match the
        ``files`` table -- that is keyed on the name as the directory walk saw it.
        Going through the listing means this returns the same answer on a
        case-folding and a case-sensitive filesystem, so an index built on one
        platform is identical to one built on the other.
        """
        listing = self._listing(candidate.parent)
        real = listing.get(candidate.name.lower())
        if real is None:
            # Either genuinely absent, or the parent could not be listed at all
            # (a permission error); fall back to asking the filesystem directly.
            return candidate if candidate.exists() else None
        if real != candidate.name:
            self.case_resolved += 1
        return candidate.with_name(real)

    def resolve_file(self, candidate: Path) -> Optional[Path]:
        """As :meth:`resolve`, but only for a path that is a regular file."""
        p = self.resolve(candidate)
        return p if p is not None and p.is_file() else None

    def invalidate(self, directory: Path) -> None:
        self._dirs.pop(directory.as_posix(), None)


#: Process-wide default.  The indexer walks every directory once, so sharing one
#: cache across the markup context, the designer check and the Web.config lookup
#: keeps the total cost at one ``scandir`` per directory that needs one at all.
DEFAULT = CaseMap()
