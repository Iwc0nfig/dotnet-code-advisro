"""Portable source analysis and regression checks."""

from __future__ import annotations

from tree_sitter import Node, Parser, Query, QueryCursor
from tree_sitter_language_pack import get_language

from .extract import EdgeRow, FileExtract, RefRow, SymbolRow, collapse

CS = get_language("csharp")
_parser = Parser(CS)

# Node type -> symbols.kind
TYPE_KINDS: dict[str, str] = {
    "class_declaration": "class",
    "struct_declaration": "struct",
    "interface_declaration": "interface",
    "record_declaration": "record",
    "enum_declaration": "enum",
    "delegate_declaration": "delegate",
}
MEMBER_KINDS: dict[str, str] = {
    "method_declaration": "method",
    "local_function_statement": "method",
    "constructor_declaration": "constructor",
    "destructor_declaration": "method",
    "operator_declaration": "method",
    "conversion_operator_declaration": "method",
    "property_declaration": "property",
    "event_declaration": "event",
    "event_field_declaration": "event",
    "field_declaration": "field",
    "enum_member_declaration": "field",
    "indexer_declaration": "indexer",
}
NS_KINDS = ("namespace_declaration", "file_scoped_namespace_declaration")

# Node types that open a naming scope, for the container walk.
SCOPE_TYPES = frozenset(NS_KINDS) | frozenset(TYPE_KINDS)

DEFS_SCM = r"""
(namespace_declaration name: (_) @ns.name) @ns.def
(file_scoped_namespace_declaration name: (_) @ns.name) @ns.def
(class_declaration     name: (identifier) @type.name) @type.def
(struct_declaration    name: (identifier) @type.name) @type.def
(interface_declaration name: (identifier) @type.name) @type.def
(record_declaration    name: (identifier) @type.name) @type.def
(enum_declaration      name: (identifier) @type.name) @type.def
(delegate_declaration  name: (identifier) @type.name) @type.def
(method_declaration    name: (identifier) @member.name
                       parameters: (parameter_list) @member.params) @member.def
(local_function_statement name: (identifier) @member.name
                       parameters: (parameter_list) @member.params) @member.def
(constructor_declaration name: (identifier) @member.name
                       parameters: (parameter_list) @member.params) @member.def
(destructor_declaration name: (identifier) @member.name) @member.def
(operator_declaration  parameters: (parameter_list) @member.params) @member.def
(property_declaration  name: (identifier) @member.name) @member.def
(event_declaration     name: (identifier) @member.name) @member.def
(event_field_declaration (variable_declaration (variable_declarator
                       name: (identifier) @member.name))) @member.def
(field_declaration (variable_declaration (variable_declarator
                       name: (identifier) @member.name))) @member.def
(enum_member_declaration name: (identifier) @member.name) @member.def
(indexer_declaration) @member.def
"""

USES_SCM = r"""
(invocation_expression function: (identifier) @call.name) @call.site
(invocation_expression
   function: (member_access_expression
                expression: (_) @call.recv
                name: (identifier) @call.name)) @call.site
(object_creation_expression type: (_) @new.type) @new.site
(using_directive (_) @using.name)
(identifier) @ident
"""

_defs_q = Query(CS, DEFS_SCM)
_uses_q = Query(CS, USES_SCM)

# Identifier nodes whose parent field makes them a declaration, not a use.
_DECL_PARENTS = frozenset(
    list(TYPE_KINDS)
    + list(MEMBER_KINDS)
    + ["variable_declarator", "parameter", "type_parameter", "catch_declaration"]
)


def parse(src: bytes):
    return _parser.parse(src)


# --- helpers ---------------------------------------------------------------


def _text(src: bytes, node: Node) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _line(node: Node) -> int:
    return node.start_point[0] + 1


def _col(node: Node) -> int:
    return node.start_point[1] + 1


def _signature(src: bytes, node: Node) -> str:
    """Declaration header: start to the first '{', '=>', ';' or newline."""
    stop = node.end_byte
    i = node.start_byte
    while i < stop:
        c = src[i]
        if c in (0x7B, 0x3B, 0x0A):  # { ; \n
            break
        if c == 0x3D and i + 1 < stop and src[i + 1] == 0x3E:  # =>
            break
        i += 1
    return collapse(src[node.start_byte : i].decode("utf-8", "replace"))


def _modifiers(src: bytes, node: Node) -> str | None:
    mods = [_text(src, ch) for ch in node.children if ch.type == "modifier"]
    return " ".join(mods) if mods else None


def _doc_comment(src: bytes, node: Node) -> str | None:
    """Contiguous ``///`` lines immediately above the declaration."""
    lines: list[str] = []
    prev = node.prev_sibling
    while prev is not None and prev.type == "comment":
        txt = _text(src, prev).strip()
        if not txt.startswith("///"):
            break
        lines.append(txt.lstrip("/").strip())
        prev = prev.prev_sibling
    if not lines:
        return None
    return collapse(" ".join(reversed(lines)), 1000)


def _arity(node: Node) -> int | None:
    params = node.child_by_field_name("parameters")
    if params is None:
        for ch in node.children:
            if ch.type == "parameter_list":
                params = ch
                break
    if params is None:
        return None
    return sum(1 for ch in params.children if ch.type == "parameter")


def _container_of(src: bytes, node: Node) -> str | None:
    """Walk parents collecting namespace and type names (rule 2)."""
    parts: list[str] = []
    cur = node.parent
    while cur is not None:
        if cur.type in SCOPE_TYPES:
            nm = cur.child_by_field_name("name")
            if nm is not None:
                parts.append(_text(src, nm))
        cur = cur.parent
    if not parts:
        return None
    return ".".join(reversed(parts))


def _bases(src: bytes, node: Node) -> list[str]:
    out: list[str] = []
    for ch in node.children:
        if ch.type == "base_list":
            for b in ch.children:
                if b.type in (",", ":"):
                    continue
                out.append(_text(src, b).strip())
    return out


def _enclosing_index(node: Node, spans: list[tuple[int, int, int]]) -> int | None:
    """Innermost member/type symbol whose byte span contains this node."""
    best: int | None = None
    best_len = 1 << 62
    pos = node.start_byte
    for idx, (bs, be, _) in enumerate(spans):
        if bs <= pos < be and (be - bs) < best_len:
            best, best_len = idx, be - bs
    return best


# --- main entry points -----------------------------------------------------


def extract_symbols(src: bytes, *, is_generated: bool = False) -> FileExtract:
    """Pass 1: declarations only."""
    fx = FileExtract()
    tree = parse(src)
    if tree.root_node.has_error:
        fx.parse_status = "partial"

    matches = QueryCursor(_defs_q).matches(tree.root_node)
    rows: list[tuple[int, SymbolRow]] = []

    for _, caps in matches:
        def_nodes = caps.get("ns.def") or caps.get("type.def") or caps.get("member.def")
        if not def_nodes:
            continue
        node = def_nodes[0]
        ntype = node.type

        if ntype in NS_KINDS:
            kind = "namespace"
        elif ntype in TYPE_KINDS:
            kind = TYPE_KINDS[ntype]
        else:
            kind = MEMBER_KINDS.get(ntype)
        if kind is None:
            continue

        name_nodes = caps.get("ns.name") or caps.get("type.name") or caps.get("member.name")
        if name_nodes:
            name = _text(src, name_nodes[0])
        elif ntype == "indexer_declaration":
            name = "this[]"
        elif ntype in ("operator_declaration", "conversion_operator_declaration"):
            op = node.child_by_field_name("operator")
            name = "operator " + (_text(src, op) if op is not None else "?")
        else:
            continue

        container = _container_of(src, node)
        if kind == "namespace":
            fqn = ".".join(p for p in (container, name) if p)
        else:
            fqn = f"{container}.{name}" if container else name

        rows.append(
            (
                node.start_byte,
                SymbolRow(
                    kind=kind,
                    name=name,
                    container=container,
                    fqn=fqn,
                    signature=_signature(src, node),
                    arity=_arity(node) if kind in ("method", "constructor", "delegate") else None,
                    modifiers=_modifiers(src, node),
                    doc_comment=_doc_comment(src, node),
                    line_start=_line(node),
                    line_end=node.end_point[0] + 1,
                    col_start=_col(node),
                    byte_start=node.start_byte,
                    byte_end=node.end_byte,
                    is_generated=is_generated,
                    bases=_bases(src, node) if kind in TYPE_KINDS.values() else [],
                ),
            )
        )

    rows.sort(key=lambda r: r[0])
    fx.symbols = [r for _, r in rows]

    # Base types become inherits/implements edges once the target kind is known.
    for i, s in enumerate(fx.symbols):
        for b in s.bases:
            fx.edges.append(
                EdgeRow(
                    edge_type="inherits",
                    evidence=collapse(f"{s.signature}", 200),
                    dst_name=b,
                    src_sym=i,
                    line=s.line_start,
                )
            )
    return fx


def extract_refs(src: bytes, symbols: list[SymbolRow], keep_names: frozenset[str]) -> list[RefRow]:
    """Pass 2: use sites.

    ``keep_names`` bounds the identifier firehose: a bare identifier is recorded
    only when some declaration in this repository carries that name.  Calls,
    ``new`` and ``using`` are always recorded.  Locals, parameters and BCL names
    that match nothing in the repo are dropped -- they cannot resolve to a
    repository symbol, so a row for them would be noise, not grounding.
    """
    tree = parse(src)
    spans = [(s.byte_start, s.byte_end, i) for i, s in enumerate(symbols)]
    out: list[RefRow] = []
    seen: set[tuple[str, int, int]] = set()

    for _, caps in QueryCursor(_uses_q).matches(tree.root_node):
        if "call.name" in caps:
            n = caps["call.name"][0]
            recv = caps.get("call.recv")
            out.append(
                RefRow(
                    name=_text(src, n),
                    receiver=collapse(_text(src, recv[0]), 120) if recv else None,
                    ref_kind="call",
                    line=_line(n),
                    col=_col(n),
                    byte_start=n.start_byte,
                    enclosing=_enclosing_index(n, spans),
                )
            )
            seen.add((_text(src, n), _line(n), _col(n)))
        elif "new.type" in caps:
            n = caps["new.type"][0]
            out.append(
                RefRow(
                    name=_simple_type_name(_text(src, n)),
                    ref_kind="new",
                    line=_line(n),
                    col=_col(n),
                    byte_start=n.start_byte,
                    enclosing=_enclosing_index(n, spans),
                )
            )
            seen.add((_simple_type_name(_text(src, n)), _line(n), _col(n)))
        elif "using.name" in caps:
            n = caps["using.name"][0]
            out.append(
                RefRow(
                    name=_text(src, n),
                    ref_kind="using",
                    line=_line(n),
                    col=_col(n),
                    byte_start=n.start_byte,
                    enclosing=None,
                )
            )
        elif "ident" in caps:
            n = caps["ident"][0]
            name = _text(src, n)
            if name not in keep_names:
                continue
            parent = n.parent
            if parent is not None:
                if parent.type in _DECL_PARENTS and parent.child_by_field_name("name") == n:
                    continue
                if parent.type == "member_access_expression" and parent.child_by_field_name("name") == n:
                    key = (name, _line(n), _col(n))
                    if key in seen:
                        continue
                    out.append(
                        RefRow(
                            name=name,
                            receiver=collapse(
                                _text(src, parent.child_by_field_name("expression")), 120
                            )
                            if parent.child_by_field_name("expression") is not None
                            else None,
                            ref_kind="member_access",
                            line=_line(n),
                            col=_col(n),
                            byte_start=n.start_byte,
                            enclosing=_enclosing_index(n, spans),
                        )
                    )
                    seen.add(key)
                    continue
            key = (name, _line(n), _col(n))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                RefRow(
                    name=name,
                    ref_kind="identifier",
                    line=_line(n),
                    col=_col(n),
                    byte_start=n.start_byte,
                    enclosing=_enclosing_index(n, spans),
                )
            )
    return out


def _simple_type_name(text: str) -> str:
    """``Demo.UI.Foo<Bar>`` -> ``Foo``.  Generic and namespace decoration is
    dropped so the name can be matched against ``symbols.name``."""
    t = text.strip()
    if "<" in t:
        t = t[: t.index("<")]
    if "." in t:
        t = t.rsplit(".", 1)[1]
    return t.strip()
