"""Shared row shapes produced by every extractor.

These are *file-local*: names, not database ids.  ``runner.py`` assigns ids and
resolves cross-file targets.  Keeping extractors id-free is what lets a single
file be re-indexed on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class SymbolRow:
    kind: str
    name: str
    line_start: int
    line_end: int
    col_start: int
    byte_start: int
    byte_end: int
    container: Optional[str] = None
    fqn: Optional[str] = None
    signature: Optional[str] = None
    arity: Optional[int] = None
    modifiers: Optional[str] = None
    doc_comment: Optional[str] = None
    is_generated: bool = False
    is_synthetic: bool = False
    # Base types as written, for the resolve step to turn into inherits/implements.
    bases: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RefRow:
    name: str
    ref_kind: str
    line: int
    col: int
    byte_start: int
    receiver: Optional[str] = None
    # Index into the file's symbol list, or None when at file scope.
    enclosing: Optional[int] = None


@dataclass(slots=True)
class EdgeRow:
    edge_type: str
    evidence: str
    dst_name: Optional[str] = None
    dst_kind: str = "symbol"
    # Index into the file's symbol list; None means the edge starts at the file.
    src_sym: Optional[int] = None
    line: Optional[int] = None
    confidence: str = "exact"
    # Set when the destination is a file path rather than a symbol name.
    dst_path: Optional[str] = None


@dataclass(slots=True)
class FileExtract:
    symbols: list[SymbolRow] = field(default_factory=list)
    refs: list[RefRow] = field(default_factory=list)
    edges: list[EdgeRow] = field(default_factory=list)
    parse_status: str = "ok"


def collapse(text: str, limit: int = 300) -> str:
    """Whitespace-collapse and truncate a source snippet."""
    out = " ".join(text.split())
    return out[:limit]
