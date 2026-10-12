"""Portable source analysis and regression checks."""

from __future__ import annotations

from tree_sitter import Node, Parser, Query, QueryCursor
from tree_sitter_language_pack import get_language

from .extract import FileExtract, RefRow, SymbolRow, collapse

JS = get_language("javascript")
_parser = Parser(JS)

DEFS_SCM = r"""
(function_declaration name: (identifier) @fn.name) @fn.def
(generator_function_declaration name: (identifier) @fn.name) @fn.def
(class_declaration name: (identifier) @class.name) @class.def
(method_definition name: (property_identifier) @method.name) @method.def
(variable_declarator name: (identifier) @var.name
   value: [(function_expression) (arrow_function)]) @fn.def
(assignment_expression
   left: (member_expression object: (_) @ns.name
                            property: (property_identifier) @fn.name)
   right: [(function_expression) (arrow_function)]) @fn.def
(pair key: (property_identifier) @fn.name
      value: [(function_expression) (arrow_function)]) @fn.def
"""

USES_SCM = r"""
(call_expression function: (identifier) @call.name) @call.site
(call_expression
   function: (member_expression object: (_) @call.recv
                                property: (property_identifier) @call.name)) @call.site
(new_expression constructor: (identifier) @new.type) @new.site
(identifier) @ident
"""

_defs_q = Query(JS, DEFS_SCM)
_uses_q = Query(JS, USES_SCM)

# A container only yields an addressable fqn when it is a plain dotted path.
# `$("#x").children()[0].onclick = function(){}` is a real definition and is kept,
# but it has no name anything could reference, so it gets no fqn rather than a
# fabricated one.
_DOTTED = __import__("re").compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")


def _fqn(container: str | None, name: str) -> str | None:
    if container is None:
        return name
    return f"{container}.{name}" if _DOTTED.match(container) else None


def parse(src: bytes):
    return _parser.parse(src)


def _text(src: bytes, node: Node) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _line(node: Node, line_offset: int = 0) -> int:
    return node.start_point[0] + 1 + line_offset


def _col(node: Node) -> int:
    return node.start_point[1] + 1


def _signature(src: bytes, node: Node) -> str:
    stop = node.end_byte
    i = node.start_byte
    while i < stop:
        c = src[i]
        if c in (0x7B, 0x0A):  # { \n
            break
        i += 1
    return collapse(src[node.start_byte : i].decode("utf-8", "replace"))


def _class_container(src: bytes, node: Node) -> str | None:
    """Enclosing class name for a method_definition."""
    cur = node.parent
    while cur is not None:
        if cur.type in ("class_declaration", "class"):
            nm = cur.child_by_field_name("name")
            if nm is not None:
                return _text(src, nm)
            return None
        cur = cur.parent
    return None


def extract_symbols(
    src: bytes, *, line_offset: int = 0, byte_offset: int = 0
) -> FileExtract:
    """Declarations in a JS source.

    ``line_offset``/``byte_offset`` let an inline ``<script>`` block report
    positions in its host markup file rather than in the extracted fragment.
    """
    fx = FileExtract()
    tree = parse(src)
    if tree.root_node.has_error:
        fx.parse_status = "partial"

    rows: list[tuple[int, SymbolRow]] = []
    for _, caps in QueryCursor(_defs_q).matches(tree.root_node):
        def_nodes = caps.get("fn.def") or caps.get("class.def") or caps.get("method.def")
        if not def_nodes:
            continue
        node = def_nodes[0]

        if "class.name" in caps:
            kind, name, container = "js_class", _text(src, caps["class.name"][0]), None
        elif "method.name" in caps:
            kind = "js_method"
            name = _text(src, caps["method.name"][0])
            container = _class_container(src, node)
        elif "ns.name" in caps and "fn.name" in caps:
            kind = "js_function"
            name = _text(src, caps["fn.name"][0])
            container = collapse(_text(src, caps["ns.name"][0]), 200)
        elif "fn.name" in caps:
            kind, name, container = "js_function", _text(src, caps["fn.name"][0]), None
        elif "var.name" in caps:
            kind, name, container = "js_function", _text(src, caps["var.name"][0]), None
        else:
            continue

        if not name:
            continue

        rows.append(
            (
                node.start_byte,
                SymbolRow(
                    kind=kind,
                    name=name,
                    container=container,
                    fqn=_fqn(container, name),
                    signature=_signature(src, node),
                    line_start=_line(node, line_offset),
                    line_end=node.end_point[0] + 1 + line_offset,
                    col_start=_col(node),
                    byte_start=node.start_byte + byte_offset,
                    byte_end=node.end_byte + byte_offset,
                ),
            )
        )

    rows.sort(key=lambda r: r[0])
    fx.symbols = [r for _, r in rows]
    return fx


def extract_refs(
    src: bytes,
    symbols: list[SymbolRow],
    keep_names: frozenset[str],
    *,
    line_offset: int = 0,
    byte_offset: int = 0,
) -> list[RefRow]:
    tree = parse(src)
    spans = [(s.byte_start - byte_offset, s.byte_end - byte_offset) for s in symbols]
    out: list[RefRow] = []
    seen: set[tuple[str, int, int]] = set()

    def enclosing(node: Node) -> int | None:
        best, best_len = None, 1 << 62
        pos = node.start_byte
        for i, (bs, be) in enumerate(spans):
            if bs <= pos < be and (be - bs) < best_len:
                best, best_len = i, be - bs
        return best

    for _, caps in QueryCursor(_uses_q).matches(tree.root_node):
        if "call.name" in caps:
            n = caps["call.name"][0]
            recv = caps.get("call.recv")
            name = _text(src, n)
            key = (name, _line(n, line_offset), _col(n))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                RefRow(
                    name=name,
                    receiver=collapse(_text(src, recv[0]), 120) if recv else None,
                    ref_kind="call",
                    line=_line(n, line_offset),
                    col=_col(n),
                    byte_start=n.start_byte + byte_offset,
                    enclosing=enclosing(n),
                )
            )
        elif "new.type" in caps:
            n = caps["new.type"][0]
            name = _text(src, n)
            key = (name, _line(n, line_offset), _col(n))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                RefRow(
                    name=name,
                    ref_kind="new",
                    line=_line(n, line_offset),
                    col=_col(n),
                    byte_start=n.start_byte + byte_offset,
                    enclosing=enclosing(n),
                )
            )
        elif "ident" in caps:
            n = caps["ident"][0]
            name = _text(src, n)
            if name not in keep_names:
                continue
            parent = n.parent
            if parent is not None and parent.child_by_field_name("name") == n:
                continue
            key = (name, _line(n, line_offset), _col(n))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                RefRow(
                    name=name,
                    ref_kind="identifier",
                    line=_line(n, line_offset),
                    col=_col(n),
                    byte_start=n.start_byte + byte_offset,
                    enclosing=enclosing(n),
                )
            )
    return out
