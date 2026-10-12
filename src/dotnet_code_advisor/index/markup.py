"""ASP.NET WebForms markup scanner.

Why not a grammar: ``tree-sitter-embedded-template`` classifies ``<%# ... %>``
(a WebForms *data-binding* expression) as a comment, and reads ``<%-- server
comment --%>`` as an output directive -- i.e. it would treat commented-out code
as live.  Both are unacceptable here, so the ``<% %>`` family is handled by an
exact hand-written region scanner and tags are scanned with a linear,
quote-aware walker.

The tag scan runs over the raw buffer with a mask of non-content regions rather
than over the content regions alone, because a single tag legitimately spans a
region boundary in this codebase:

    <script src='<%= Helpers.ResolvePublicUrl("js/x.js", true) %>'></script>
"""

from __future__ import annotations

import bisect
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Literal, Optional

from . import fsmap
from ..config import Config
from .extract import collapse

RegionKind = Literal[
    "content", "directive", "expr", "databind", "appsetting", "comment", "code"
]

# Longest prefix first -- order matters.
_OPENERS: list[tuple[bytes, RegionKind, bytes]] = [
    (b"<%--", "comment", b"--%>"),  # ASP.NET server comment
    (b"<%@", "directive", b"%>"),  # Page / Control / Master / Register / Import
    (b"<%=", "expr", b"%>"),  # Response.Write shortcut
    (b"<%#", "databind", b"%>"),  # data-binding expression (NOT a comment)
    (b"<%$", "appsetting", b"%>"),  # expression builder, <%$ Resources: ... %>
    (b"<%", "code", b"%>"),  # raw code render block
]


@dataclass(frozen=True)
class Region:
    kind: RegionKind
    body: bytes  # text strictly between opener and closer
    byte_start: int  # index of the opener's first byte
    byte_end: int  # index just past the closer's last byte
    line: int  # 1-based line of byte_start

    @property
    def body_start(self) -> int:
        for opener, kind, _ in _OPENERS:
            if kind == self.kind:
                return self.byte_start + len(opener)
        return self.byte_start


def scan_regions(src: bytes) -> Iterator[Region]:
    """Split WebForms markup into content and ``<% %>`` regions.

    Total, non-overlapping, ordered.  An unterminated opener is emitted as a
    region running to EOF rather than being dropped.
    """
    n, i = len(src), 0
    seg_start = 0
    while i < n:
        if src[i] == 0x3C:  # '<'
            for opener, kind, closer in _OPENERS:
                if src.startswith(opener, i):
                    if i > seg_start:
                        yield Region(
                            "content",
                            src[seg_start:i],
                            seg_start,
                            i,
                            1 + src.count(b"\n", 0, seg_start),
                        )
                    end = src.find(closer, i + len(opener))
                    if end == -1:
                        end_full, body_end = n, n
                    else:
                        end_full, body_end = end + len(closer), end
                    yield Region(
                        kind,
                        src[i + len(opener) : body_end],
                        i,
                        end_full,
                        1 + src.count(b"\n", 0, i),
                    )
                    i = seg_start = end_full
                    break
            else:
                i += 1
        else:
            i += 1
    if seg_start < n:
        yield Region(
            "content", src[seg_start:n], seg_start, n, 1 + src.count(b"\n", 0, seg_start)
        )


# --- 7.3 directive and tag parsing -----------------------------------------

_ATTR = re.compile(rb"""([A-Za-z_][\w:.\-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_TAGOPEN = re.compile(rb"<([A-Za-z_]\w*)(?::([\w.]+))?(?=[\s/>])")
_DIRECTIVE_NAME = re.compile(rb"^\s*([A-Za-z_]\w*)")
_RESOLVE_PUBLIC = re.compile(r'ResolvePublicUrl\(\s*"([^"]+)"')
# ResolveUrl / ResolveClientUrl / Page.ResolveClientUrl: the argument is an
# app-root-relative literal, so the target is still exact.
_RESOLVE_ANY = re.compile(r'Resolve\w*Url\(\s*"([^"]+)"')
_JSCALL = re.compile(r"^\s*(?:return\s+)?([A-Za-z_$][\w$.]*)\s*\(")


_ATTR_NAME = re.compile(rb"[A-Za-z_][\w:.\-]*")


def attrs_of(chunk: bytes) -> dict[str, str]:
    """Attribute names are lower-cased (ASP.NET markup is case-insensitive);
    values keep their original case.

    Hand-written rather than a regex because a quoted value legitimately
    contains the quote character that delimits it:

        src="<%= Helpers.ResolvePublicUrl("js/x.js", true) %>"

    ASP.NET tolerates this -- the server evaluates ``<% %>`` before the browser
    ever parses the attribute -- so a ``"([^"]*)"`` match truncates the value at
    the inner quote and silently loses the script target.  A ``<% %>`` span is
    therefore jumped over whole, regardless of the quotes inside it.
    """
    out: dict[str, str] = {}
    n, i = len(chunk), 0

    def skip_expr(j: int) -> int | None:
        if chunk.startswith(b"<%", j):
            end = chunk.find(b"%>", j + 2)
            return n if end == -1 else end + 2
        return None

    while i < n:
        m = _ATTR_NAME.search(chunk, i)
        if not m:
            break
        name = m.group(0).decode("utf-8", "replace").lower()
        j = m.end()
        while j < n and chunk[j : j + 1].isspace():
            j += 1
        if j >= n or chunk[j] != 0x3D:  # '='
            i = m.end()
            continue
        j += 1
        while j < n and chunk[j : j + 1].isspace():
            j += 1
        if j >= n:
            break
        quote = chunk[j]
        if quote in (0x22, 0x27):
            j += 1
            start = j
            while j < n:
                nxt = skip_expr(j)
                if nxt is not None:
                    j = nxt
                    continue
                if chunk[j] == quote:
                    break
                j += 1
            value = chunk[start:j]
            i = j + 1
        else:
            start = j
            while j < n and not chunk[j : j + 1].isspace() and chunk[j] not in (0x3E, 0x2F):
                nxt = skip_expr(j)
                j = nxt if nxt is not None else j + 1
            value = chunk[start:j]
            i = j
        if name not in out:
            out[name] = value.decode("utf-8", "replace")
    return out


def find_tag_end(src: bytes, i: int) -> int:
    """*i* points just past the tag name.  Returns the index just past ``>``,
    quote-aware, jumping ``<% %>`` spans whole.  ``-1`` if unterminated.

    Linear by construction.  A backtracking regex of the form
    ``(?:"[^"]*"|'[^']*'|[^>])*?>`` does not finish on these files.
    """
    n, q = len(src), 0
    while i < n:
        if src.startswith(b"<%", i):
            end = src.find(b"%>", i + 2)
            i = n if end == -1 else end + 2
            continue
        c = src[i]
        if q:
            if c == q:
                q = 0
        elif c in (0x22, 0x27):
            q = c
        elif c == 0x3E:
            return i + 1
        i += 1
    return -1


def _find_gt(src: bytes, i: int) -> int:
    """First ``>`` at or after *i*, ignoring quotes but still jumping ``<% %>``."""
    n = len(src)
    while i < n:
        if src.startswith(b"<%", i):
            end = src.find(b"%>", i + 2)
            i = n if end == -1 else end + 2
            continue
        if src[i] == 0x3E:
            return i + 1
        i += 1
    return -1


def tag_end(src: bytes, i: int) -> tuple[int, bool]:
    """Tag end with recovery.  Returns ``(end, recovered)``.

    Some markup here carries a doubled closing quote --
    ``title="<%= x %>"" style="display:none"`` -- which desynchronises any
    quote-aware scan and makes it swallow the rest of the file.  When the
    quote-aware end lies beyond the start of a later tag, the scan has clearly
    desynchronised, so the first bare ``>`` is used instead and the file is
    reported as ``parse_status='partial'`` rather than silently mis-scanned.
    """
    strict = find_tag_end(src, i)
    naive = _find_gt(src, i)
    if strict == -1:
        return (naive, True) if naive != -1 else (-1, True)
    # ``</`` rather than a bare ``<`` on purpose: an attribute value may
    # legitimately contain ``<`` (``data-expr="p<q"``), but no well-formed start
    # tag contains a closing tag.
    if naive != -1 and naive < strict and src.find(b"</", naive, strict) != -1:
        return naive, True
    return strict, False


class LineIndex:
    """Byte offset -> 1-based line number, in O(log n)."""

    __slots__ = ("starts",)

    def __init__(self, src: bytes) -> None:
        starts = [0]
        i = src.find(b"\n")
        while i != -1:
            starts.append(i + 1)
            i = src.find(b"\n", i + 1)
        self.starts = starts

    def line(self, pos: int) -> int:
        return bisect.bisect_right(self.starts, pos)

    def col(self, pos: int) -> int:
        return pos - self.starts[self.line(pos) - 1] + 1


# --- 7.4 control type resolution -------------------------------------------

HTML_SERVER_MAP: dict[str, str | None] = {
    "a": "System.Web.UI.HtmlControls.HtmlAnchor",
    "button": "System.Web.UI.HtmlControls.HtmlButton",
    "form": "System.Web.UI.HtmlControls.HtmlForm",
    "head": "System.Web.UI.HtmlControls.HtmlHead",
    "img": "System.Web.UI.HtmlControls.HtmlImage",
    "input": None,  # decided by the type= attribute
    "select": "System.Web.UI.HtmlControls.HtmlSelect",
    "table": "System.Web.UI.HtmlControls.HtmlTable",
    "td": "System.Web.UI.HtmlControls.HtmlTableCell",
    "th": "System.Web.UI.HtmlControls.HtmlTableCell",
    "tr": "System.Web.UI.HtmlControls.HtmlTableRow",
    "textarea": "System.Web.UI.HtmlControls.HtmlTextArea",
    "title": "System.Web.UI.HtmlControls.HtmlTitle",
    "link": "System.Web.UI.HtmlControls.HtmlLink",
    "meta": "System.Web.UI.HtmlControls.HtmlMeta",
    "iframe": "System.Web.UI.HtmlControls.HtmlIframe",
    "audio": "System.Web.UI.HtmlControls.HtmlAudio",
    "video": "System.Web.UI.HtmlControls.HtmlVideo",
    "source": "System.Web.UI.HtmlControls.HtmlSource",
    "track": "System.Web.UI.HtmlControls.HtmlTrack",
    "embed": "System.Web.UI.HtmlControls.HtmlEmbed",
    "area": "System.Web.UI.HtmlControls.HtmlArea",
}
HTML_INPUT_MAP = {
    "text": "HtmlInputText",
    "password": "HtmlInputPassword",
    "hidden": "HtmlInputHidden",
    "checkbox": "HtmlInputCheckBox",
    "radio": "HtmlInputRadioButton",
    "file": "HtmlInputFile",
    "image": "HtmlInputImage",
    "button": "HtmlInputButton",
    "submit": "HtmlInputSubmit",
    "reset": "HtmlInputReset",
}
FALLBACK_TYPE = "System.Web.UI.Control"


def html_server_type(tag: str, attrs: dict[str, str]) -> str:
    tag = tag.lower()
    if tag == "input":
        t = attrs.get("type", "text").lower()
        return "System.Web.UI.HtmlControls." + HTML_INPUT_MAP.get(
            t, "HtmlInputGenericControl"
        )
    return HTML_SERVER_MAP.get(tag) or "System.Web.UI.HtmlControls.HtmlGenericControl"


TEMPLATE_TAGS = frozenset(
    t.lower()
    for t in (
        "ItemTemplate",
        "EditItemTemplate",
        "HeaderTemplate",
        "AlternatingItemTemplate",
        "FooterTemplate",
        "InsertItemTemplate",
        "LayoutTemplate",
        "ContentTemplate",
        "SelectedItemTemplate",
        "EmptyDataTemplate",
        "PagerTemplate",
    )
)


# --- data carriers ---------------------------------------------------------


@dataclass
class ControlInfo:
    control_ref: str  # "democtrls:LookupControlEx" or "input"
    tag_prefix: str  # "" for plain HTML tags
    tag_name: str
    ctl_id: Optional[str]
    resolved_type: Optional[str]
    resolution: str
    line: int
    col: int
    byte_start: int
    attrs: dict[str, str] = field(default_factory=dict)
    in_template: bool = False
    server_handlers: list[tuple[str, str]] = field(default_factory=list)  # (attr, method)
    client_handlers: list[tuple[str, str]] = field(default_factory=list)  # (attr, value)


@dataclass
class RegisterDecl:
    tag_prefix: str
    tag_name: Optional[str]
    src: Optional[str]
    namespace: Optional[str]
    assembly: Optional[str]
    line: int


@dataclass
class ScriptInclude:
    raw: str
    resolved_rel: Optional[str]
    line: int
    confidence: str


@dataclass
class InlineScript:
    body: bytes
    byte_start: int
    line: int


@dataclass
class PageInfo:
    directive: str = "page"  # page | control | master
    inherits: Optional[str] = None
    code_file: Optional[str] = None
    code_attr: Optional[str] = None  # 'codefile' | 'codebehind'
    master_page: Optional[str] = None
    master_type: Optional[str] = None  # resolved .NET type name
    master_type_raw: Optional[str] = None  # VirtualPath or TypeName as written
    language: Optional[str] = None
    auto_event_wireup: Optional[str] = None
    directive_line: int = 1
    directive_text: str = ""
    imports: list[tuple[str, int]] = field(default_factory=list)
    registers: list[RegisterDecl] = field(default_factory=list)
    references: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class MarkupExtract:
    page: Optional[PageInfo]
    controls: list[ControlInfo] = field(default_factory=list)
    scripts: list[ScriptInclude] = field(default_factory=list)
    styles: list[ScriptInclude] = field(default_factory=list)
    inline_scripts: list[InlineScript] = field(default_factory=list)
    expressions: list[Region] = field(default_factory=list)
    tag_count: int = 0
    server_tag_count: int = 0
    html_runat_count: int = 0
    parse_status: str = "ok"


# --- the resolution context ------------------------------------------------


class MarkupContext:
    """Everything a single markup file needs from outside itself.

    Built once per index pass: the ``App/Web.config`` ``<controls>`` map, the
    reflected BCL control tables, and a lazily-filled ``.ascx`` -> ``Inherits``
    cache.
    """

    def __init__(self, cfg: Config, casemap: Optional[fsmap.CaseMap] = None) -> None:
        self.cfg = cfg
        # Shared with the designer check and the indexer so one scandir per
        # directory serves all of them.
        self.casemap = casemap or fsmap.CaseMap()
        self.prefix_ns: dict[str, str] = {}
        self.prefix_tag_src: dict[tuple[str, str], str] = {}
        self.bcl_web: dict[str, str] = {}
        self.bcl_html: dict[str, str] = {}
        self.bcl_ui: dict[str, str] = {}
        self._ci: list[dict[str, str]] = []  # lower-cased mirrors of the three tables
        self.ascx_inherits: dict[str, Optional[str]] = {}
        self.web_config_error: Optional[str] = None
        self.bcl_error: Optional[str] = None
        self._load_bcl()
        self._load_web_config()

    # -- loading ----------------------------------------------------------

    def _load_bcl(self) -> None:
        p = self.cfg.paths.aspnet_controls_json
        if not p.is_file():
            self.bcl_error = (
                f"{p.name} not generated. It is produced on Windows by "
                "scripts/windows/gen_aspnet_controls.ps1, which reflects over the "
                ".NET Framework reference assemblies, and ships inside .index/. "
                "asp: tags will fall back to System.Web.UI.Control."
            )
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            self.bcl_web = dict(data.get("webcontrols") or {})
            self.bcl_html = dict(data.get("htmlcontrols") or {})
            self.bcl_ui = dict(data.get("uicontrols") or {})
            # ASP.NET markup is case-insensitive: <asp:label> is <asp:Label>.
            for table in (self.bcl_web, self.bcl_html, self.bcl_ui):
                self._ci.append({k.lower(): v for k, v in table.items()})
        except Exception as exc:  # pragma: no cover - defensive
            self.bcl_error = f"{p.name} unreadable: {exc}"

    def _load_web_config(self) -> None:
        if self.cfg.paths.website_root is None:
            return
        wc = self.casemap.resolve_file(self.cfg.paths.website_root / "Web.config")
        if wc is None or not self.cfg.contains_source(wc):
            self.web_config_error = f"{self.cfg.paths.website_root / 'Web.config'} not found"
            return
        try:
            root = ET.fromstring(wc.read_text(encoding="utf-8-sig", errors="replace"))
        except ET.ParseError as exc:
            self.web_config_error = f"Web.config unparsable: {exc}"
            return
        for add in root.iter("add"):
            tp = add.get("tagPrefix")
            if not tp:
                continue
            tp_l = tp.lower()
            ns, src, tn = add.get("namespace"), add.get("src"), add.get("tagName")
            if ns:
                self.prefix_ns[tp_l] = ns
            if src and tn:
                self.prefix_tag_src[(tp_l, tn.lower())] = src

    # -- helpers ----------------------------------------------------------

    def virtual_to_path(self, virtual: str, markup_file: Path) -> Optional[Path]:
        """Map ``~/x/y.ascx`` or a relative path to an absolute path.

        The case written in markup is not always the case on disk -- ASP.NET does
        not care, and on NTFS neither does the filesystem.  Resolution therefore
        goes through the case map, so that ``~/system/templates/exContent.master``
        still finds ``excontent.master`` on a case-sensitive filesystem.
        """
        v = virtual.strip().replace("\\", "/")
        if not v:
            return None
        if v.startswith(("~/", "/")) and self.cfg.paths.website_root is None:
            return None
        if v.startswith("~/"):
            cand = self.cfg.paths.website_root / v[2:]
        elif v.startswith("/"):
            cand = self.cfg.paths.website_root / v[1:]
        else:
            cand = markup_file.parent / v
        try:
            resolved = self.casemap.resolve(cand)
        except OSError:
            return None
        if resolved is None or not self.cfg.contains_source(resolved):
            return None
        try:
            return resolved.resolve()
        except OSError:
            return resolved

    def inherits_of(self, ascx_path: Path) -> Optional[str]:
        # Keyed on the path as given, NOT lowercased: two .ascx files differing
        # only in case are two different files on a case-sensitive filesystem, and
        # folding them together would hand the second one the first one's
        # Inherits class.  virtual_to_path has already mapped the written case to
        # the real one, so equal paths here really are the same file.
        key = ascx_path.as_posix()
        if key in self.ascx_inherits:
            return self.ascx_inherits[key]
        value: Optional[str] = None
        try:
            if self.cfg.contains_source(ascx_path) and ascx_path.is_file():
                head = ascx_path.read_bytes()[:8192]
                for r in scan_regions(head):
                    if r.kind != "directive":
                        continue
                    a = attrs_of(r.body)
                    if "inherits" in a:
                        value = a["inherits"]
                        break
        except OSError:
            value = None
        self.ascx_inherits[key] = value
        return value


# --- directive parsing -----------------------------------------------------

MAIN_DIRECTIVES = {"page": "page", "control": "control", "master": "master"}


def parse_directives(src: bytes, regions: list[Region]) -> PageInfo | None:
    page: PageInfo | None = None
    pending: PageInfo = PageInfo()
    saw_main = False

    for r in regions:
        if r.kind != "directive":
            continue
        m = _DIRECTIVE_NAME.match(r.body)
        if not m:
            continue
        name = m.group(1).decode("utf-8", "replace").lower()
        a = attrs_of(r.body)

        if name in MAIN_DIRECTIVES and not saw_main:
            saw_main = True
            pending.directive = MAIN_DIRECTIVES[name]
            pending.inherits = a.get("inherits")
            if "codefile" in a:
                pending.code_file, pending.code_attr = a["codefile"], "codefile"
            elif "codebehind" in a:
                pending.code_file, pending.code_attr = a["codebehind"], "codebehind"
            pending.master_page = a.get("masterpagefile")
            pending.language = a.get("language")
            pending.auto_event_wireup = a.get("autoeventwireup")
            pending.directive_line = r.line
            pending.directive_text = collapse(
                r.body.decode("utf-8", "replace"), 300
            )
        elif name == "mastertype":
            # TypeName is a .NET type; VirtualPath points at a .master whose
            # Inherits class is the type.  Resolved in scan(), which has the
            # context needed to read that file.
            pending.master_type_raw = a.get("typename") or a.get("virtualpath")
            pending.master_type = a.get("typename")
        elif name == "register":
            pending.registers.append(
                RegisterDecl(
                    tag_prefix=(a.get("tagprefix") or "").lower(),
                    tag_name=(a.get("tagname") or None),
                    src=a.get("src"),
                    namespace=a.get("namespace"),
                    assembly=a.get("assembly"),
                    line=r.line,
                )
            )
        elif name == "import":
            if a.get("namespace"):
                pending.imports.append((a["namespace"], r.line))
        elif name == "reference":
            v = a.get("control") or a.get("page") or a.get("virtualpath")
            if v:
                pending.references.append((v, r.line))

    if saw_main:
        page = pending
    return page


# --- the scanner -----------------------------------------------------------


def scan(src: bytes, markup_file: Path, ctx: MarkupContext) -> MarkupExtract:
    regions = list(scan_regions(src))
    li = LineIndex(src)

    page = parse_directives(src, regions)
    if page is not None and page.master_type is None and page.master_type_raw:
        target = ctx.virtual_to_path(page.master_type_raw, markup_file)
        if target is not None:
            page.master_type = ctx.inherits_of(target)
    ex = MarkupExtract(page=page)
    ex.expressions = [r for r in regions if r.kind in ("expr", "databind", "code", "appsetting")]

    # Byte spans the tag scan must ignore: comments, directives and code blocks.
    mask = [(r.byte_start, r.byte_end) for r in regions if r.kind != "content"]
    mask_starts = [s for s, _ in mask]

    def masked(pos: int) -> bool:
        i = bisect.bisect_right(mask_starts, pos) - 1
        return i >= 0 and pos < mask[i][1]

    # Per-file register maps, which take precedence over Web.config.
    reg_src: dict[tuple[str, str], str] = {}
    reg_ns: dict[str, str] = {}
    if page:
        for rd in page.registers:
            if rd.src and rd.tag_name:
                reg_src[(rd.tag_prefix, rd.tag_name.lower())] = rd.src
            elif rd.namespace:
                reg_ns[rd.tag_prefix] = rd.namespace

    template_depth = 0
    open_templates: list[str] = []

    for m in _TAGOPEN.finditer(src):
        start = m.start()
        if masked(start):
            continue
        end, recovered = tag_end(src, m.end())
        if recovered:
            ex.parse_status = "partial"
        if end == -1:
            continue
        raw_tag = src[start:end]
        attrs = attrs_of(raw_tag)
        prefix_b, name_b = m.group(1), m.group(2)
        prefix = prefix_b.decode("utf-8", "replace")
        ex.tag_count += 1

        if name_b is not None:
            tag_name = name_b.decode("utf-8", "replace")
            # Template tracking: <asp:GridView><ItemTemplate>...
            if tag_name.lower() in TEMPLATE_TAGS:
                if not raw_tag.endswith(b"/>"):
                    template_depth += 1
                    open_templates.append(tag_name.lower())
                continue
            ex.server_tag_count += 1
            rtype, res = _resolve_control_type(
                prefix, tag_name, attrs, markup_file, ctx, reg_src, reg_ns
            )
            ex.controls.append(
                _mk_control(
                    f"{prefix}:{tag_name}", prefix, tag_name, attrs,
                    rtype, res, li, start, template_depth > 0, server_side=True,
                )
            )
            continue

        tag_name = prefix
        low = tag_name.lower()

        if low == "script":
            _handle_script(src, end, raw_tag, attrs, li, start, markup_file, ctx, ex)
            continue
        if low == "link" and (attrs.get("rel", "").lower() == "stylesheet") and "runat" not in attrs:
            href = attrs.get("href", "")
            ex.styles.append(_resolve_asset(href, li.line(start)))
            continue

        if attrs.get("runat", "").lower() == "server":
            ex.html_runat_count += 1
            ex.controls.append(
                _mk_control(
                    tag_name, "", tag_name, attrs,
                    html_server_type(tag_name, attrs), "html_runat",
                    li, start, template_depth > 0, server_side=True,
                )
            )

    # Template nesting is resolved in a second linear pass so the open-tag loop
    # above stays a single forward scan.  Always run it: template tags are
    # normally written unprefixed, so the forward loop never sees them.
    if ex.controls:
        ex.controls = _fix_template_depth(src, ex.controls, mask, mask_starts, li)

    return ex


def _mk_control(
    control_ref: str,
    prefix: str,
    tag_name: str,
    attrs: dict[str, str],
    rtype: Optional[str],
    res: str,
    li: LineIndex,
    start: int,
    in_template: bool,
    *,
    server_side: bool,
) -> ControlInfo:
    ci = ControlInfo(
        control_ref=control_ref,
        tag_prefix=prefix,
        tag_name=tag_name,
        ctl_id=attrs.get("id"),
        resolved_type=rtype,
        resolution=res,
        line=li.line(start),
        col=li.col(start),
        byte_start=start,
        attrs=attrs,
        in_template=in_template,
    )
    if server_side:
        for k, v in attrs.items():
            if k.startswith("onclient"):
                ci.client_handlers.append((k, v))
            elif k.startswith("on") and len(k) > 2 and "<%" not in v:
                ci.server_handlers.append((k, v))
    return ci


def _fix_template_depth(src, controls, mask, mask_starts, li):
    """Recompute ``in_template`` with proper open/close tracking."""
    events: list[tuple[int, int]] = []  # (byte, +1/-1)
    # Template tags are normally written unprefixed inside a data-bound control
    # (`<asp:GridView>...<ItemTemplate>`), so the prefix is optional here.
    for m in re.finditer(rb"<(/?)(?:[A-Za-z_]\w*:)?([\w.]+)", src):
        i = bisect.bisect_right(mask_starts, m.start()) - 1
        if i >= 0 and m.start() < mask[i][1]:
            continue
        tag = m.group(2).decode("utf-8", "replace").lower()
        if tag not in TEMPLATE_TAGS:
            continue
        if m.group(1):
            events.append((m.start(), -1))
        else:
            end, _ = tag_end(src, m.end())
            if end != -1 and src[end - 2 : end] == b"/>":
                continue
            events.append((m.start(), +1))
    if not events:
        for c in controls:
            c.in_template = False
        return controls
    events.sort()
    positions = [e[0] for e in events]
    prefix_depth: list[int] = []
    d = 0
    for _, delta in events:
        d += delta
        prefix_depth.append(d)
    for c in controls:
        i = bisect.bisect_right(positions, c.byte_start) - 1
        c.in_template = i >= 0 and prefix_depth[i] > 0
    return controls


def _resolve_control_type(
    prefix: str,
    tag_name: str,
    attrs: dict[str, str],
    markup_file: Path,
    ctx: MarkupContext,
    reg_src: dict[tuple[str, str], str],
    reg_ns: dict[str, str],
) -> tuple[Optional[str], str]:
    """, in order.  The rule that fired is recorded, never inferred."""
    p, t = prefix.lower(), tag_name.lower()

    # 1. <%@ Register Src=... %> in this file
    src_v = reg_src.get((p, t))
    if src_v:
        target = ctx.virtual_to_path(src_v, markup_file)
        if target is not None:
            inh = ctx.inherits_of(target)
            if inh:
                return inh, "register_src"
        return None, "register_src"

    # 2. <%@ Register Assembly=... Namespace=... %> in this file
    ns = reg_ns.get(p)
    if ns:
        return f"{ns}.{tag_name}", "register_assembly"

    # 3. App/Web.config <controls>
    wc_src = ctx.prefix_tag_src.get((p, t))
    if wc_src:
        target = ctx.virtual_to_path(wc_src, markup_file)
        if target is not None:
            inh = ctx.inherits_of(target)
            if inh:
                return inh, "web_config"
        return None, "web_config"
    wc_ns = ctx.prefix_ns.get(p)
    if wc_ns:
        return f"{wc_ns}.{tag_name}", "web_config"

    # 4. asp: -> reflected System.Web tables.  System.Web.UI is consulted after
    # WebControls because ScriptManager/UpdatePanel/Timer live there, in
    # System.Web.Extensions rather than System.Web.
    if p == "asp":
        tl = tag_name.lower()
        for table, res in (
            (ctx.bcl_web, "bcl_webcontrols"),
            (ctx.bcl_html, "bcl_htmlcontrols"),
            (ctx.bcl_ui, "bcl_webcontrols"),
        ):
            if tag_name in table:
                return table[tag_name], res
        for ci, res in zip(ctx._ci, ("bcl_webcontrols", "bcl_htmlcontrols", "bcl_webcontrols")):
            if tl in ci:
                return ci[tl], res

    # 6. fallback
    return FALLBACK_TYPE, "fallback_control"


def _resolve_asset(raw: str, line: int) -> ScriptInclude:
    """Resolve a src/href to a path relative to the website root.

    Exact when the target is a literal, including the literal inside this
    codebase's own ``ResolvePublicUrl("js/x.js", true)`` and the framework's
    ``ResolveClientUrl("~/public/js/x.js")``.  A URL actually built at runtime
    from variables is recorded with **no** target and ``confidence:
    'heuristic'`` -- the agent is told the target is computed, never given a
    guess.
    """
    text = collapse(raw, 200)
    m = _RESOLVE_PUBLIC.search(raw)
    if m:
        return ScriptInclude(text, f"public/{m.group(1)}", line, "exact")
    m = _RESOLVE_ANY.search(raw)
    if m:
        return ScriptInclude(text, _app_relative(m.group(1)), line, "exact")
    if "<%" in raw:
        return ScriptInclude(text, None, line, "heuristic")
    if not raw.strip():
        return ScriptInclude("", None, line, "heuristic")
    return ScriptInclude(text, _app_relative(raw.strip()), line, "exact")


def _app_relative(v: str) -> str:
    v = v.strip().replace("\\", "/")
    if v.startswith("~/"):
        return v[2:]
    if v.startswith("/"):
        return v[1:]
    return v


def _handle_script(src, tag_end, raw_tag, attrs, li, start, markup_file, ctx, ex) -> None:
    if "src" in attrs:
        ex.scripts.append(_resolve_asset(attrs["src"], li.line(start)))
        return
    if raw_tag.endswith(b"/>"):
        return
    close = src.find(b"</script", tag_end)
    if close == -1:
        ex.parse_status = "partial"
        return
    body = src[tag_end:close]
    if body.strip():
        ex.inline_scripts.append(
            InlineScript(body=body, byte_start=tag_end, line=li.line(tag_end))
        )


def neutralise_server_tags(body: bytes) -> bytes:
    """Blank out ``<% ... %>`` spans so an inline script parses as JavaScript.

    Inline scripts in this codebase routinely interpolate server values:

        var currentPage = <%=currentPage%>;

    tree-sitter-javascript cannot parse that, and a single ERROR node swallows
    the rest of the block -- which silently loses every function declared below
    it.  Each span is replaced by ``0`` followed by spaces so the result is
    valid JavaScript **and exactly the same length**, keeping every byte offset
    and line number faithful to the original file.
    """
    out = bytearray(body)
    i = 0
    n = len(out)
    while i < n:
        j = body.find(b"<%", i)
        if j == -1:
            break
        end = body.find(b"%>", j + 2)
        end = n if end == -1 else end + 2
        for k in range(j, end):
            out[k] = 0x20  # space
        # A bare expression slot needs a value, not just whitespace.
        out[j] = 0x30  # '0'
        i = end
    return bytes(out)


def js_call_target(value: str) -> Optional[str]:
    """``OnClientClick="return demo.forms.save();"`` -> ``demo.forms.save``.

    Returns ``None`` for an inline statement, which is kept verbatim as evidence
    rather than being turned into a guessed function name.
    """
    m = _JSCALL.match(value)
    return m.group(1) if m else None
