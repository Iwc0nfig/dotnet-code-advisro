"""Shared output shapes.

Every tool returns a :class:`ToolEnvelope` subclass.  ``searched_scope`` and
``caveats`` are what turn "no results" into a grounded statement instead of an
invitation to guess, and ``layer`` is what stops a name-based structural match
being reported with the certainty of a compiler answer.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Side = Literal["net48", "net8"]
# roslyn        = a live compiler host answered (compiler-exact, current workspace state)
# roslyn-index  = the persisted compiler index answered (compiler-exact, as of the
#                 last build + index pass; see index_status)
Layer = Literal["roslyn", "roslyn-index", "graph", "filesystem", "mixed"]
Status = Literal["ok", "no_results", "partial", "unavailable"]
Resolution = Literal[
    "exact_fqn",
    "same_file",
    "same_type",
    "same_namespace",
    "unique_name",
    "markup_binding",
    "unresolved",
    "ambiguous",
]


class Loc(BaseModel):
    file: str = Field(description="Absolute path, forward slashes.")
    rel_path: str = Field(description="Path relative to the workspace root.")
    line_start: int = Field(description="1-based, inclusive.")
    line_end: int = Field(description="1-based, inclusive.")
    col_start: int = 1
    snippet: str = Field(
        default="", description="Verbatim source. Never paraphrased."
    )


class SymbolRef(BaseModel):
    name: str
    kind: str
    fqn: Optional[str] = None
    container: Optional[str] = None
    signature: Optional[str] = None
    side: Side
    loc: Loc
    is_generated: bool = False
    is_synthetic: bool = Field(
        default=False,
        description=(
            "True = a control field this tool synthesised from markup because "
            "ASP.NET generates it at runtime. It does NOT exist in any file on "
            "disk. Cite the markup line in loc, never the synthetic path."
        ),
    )
    confidence: Resolution = "exact_fqn"
    key: Optional[str] = Field(
        default=None,
        description=(
            "Compiler-index symbol key ('<project>|<doc-id>' or 'asm:<assembly>|<doc-id>'). "
            "Pass it back as 'key' to find_references / find_callers / "
            "find_implementations / go_to_definition to query exactly this symbol."
        ),
    )
    project: Optional[str] = None


class ProjectCompilation(BaseModel):
    """How a project compiled in the index pass.  clean=False means some uses in it did not bind."""
    project: str
    side: str
    clean: bool
    binding_errors: int = 0
    errors: int = 0
    top_errors: str = ""
    build_exit: Optional[int] = Field(
        default=None, description="Exit code of the real MSBuild build; None = not built."
    )


class ToolEnvelope(BaseModel):
    status: Status
    layer: Layer = Field(
        description=(
            "Which layer produced this. 'roslyn' and 'roslyn-index' = compiler-exact; "
            "'graph' = name-based structural match; 'filesystem' = text."
        )
    )
    searched_scope: str = Field(
        description=(
            "Exactly what was searched. Quote this when reporting that "
            "something was not found."
        )
    )
    truncated: bool = False
    total_available: Optional[int] = None
    caveats: list[str] = Field(
        default_factory=list,
        description=(
            "Machine-generated limitations that apply to THIS result. Surface "
            "them to the user verbatim; do not summarise them away."
        ),
    )
    compilation: list[ProjectCompilation] = Field(
        default_factory=list,
        description=(
            "Compile state of the projects this answer draws on (compiler layers only). "
            "Any entry with clean=false has uses the compiler could not resolve."
        ),
    )


# --- per-tool outputs -------------------------------------------------------


class FindSymbolOut(ToolEnvelope):
    query: str
    symbols: list[SymbolRef] = []


class GoToDefinitionOut(ToolEnvelope):
    target: Optional[SymbolRef] = None
    definitions: list[Loc] = []
    partial_declarations: int = 0


class FindReferencesOut(ToolEnvelope):
    target: Optional[SymbolRef] = None
    references: list[Loc] = []
    by_file: dict[str, int] = {}
    ambiguous_sites: list[Loc] = Field(
        default_factory=list,
        description=(
            "Sites where the name matched but the target could not be pinned "
            "down. NEVER merged into 'references' -- report them separately."
        ),
    )
    ambiguous_candidates: list[SymbolRef] = []
    dispatch_sites: list[Loc] = Field(
        default_factory=list,
        description=(
            "Uses of an interface or base member that this symbol implements or overrides. "
            "They may reach this symbol at run time, but the compiler bound them to the "
            "interface/base member. Report them apart from 'references'."
        ),
    )
    candidate_sites: list[Loc] = Field(
        default_factory=list,
        description=(
            "Sites where the compiler found this symbol only as a candidate (overload "
            "resolution failed, ambiguous, inaccessible...). Not references; report apart."
        ),
    )
    http_call_sites: list[Loc] = Field(
        default_factory=list,
        description=(
            "When this symbol handles an HTTP endpoint: the call sites whose URL matches "
            "its route (e.g. legacy code calling the new API). A route match, not a "
            "compiler reference -- report apart, with the match strength from caveats."
        ),
    )


class CallNode(BaseModel):
    symbol: SymbolRef
    cycle: bool = False
    http: Optional[str] = Field(
        default=None,
        description=(
            "Set when this caller reaches its parent over HTTP, not by a direct call: "
            "'<VERB> <route> <- <url as written> (<match>) at <file>:<line>'. The hop is "
            "a ROUTE MATCH between a URL string and an endpoint, not a compiler "
            "reference; <match> says how strong it is."
        ),
    )
    callers: list["CallNode"] = []


class FindCallersOut(ToolEnvelope):
    target: Optional[SymbolRef] = None
    tree: Optional[CallNode] = None
    ambiguous_callers: list[Loc] = []
    depth: int = 2


class FindImplementationsOut(ToolEnvelope):
    target: Optional[SymbolRef] = None
    implementations: list[SymbolRef] = []
    derived: list[SymbolRef] = []


class TypeInfoOut(ToolEnvelope):
    target: Optional[SymbolRef] = None
    type_kind: Optional[str] = None
    base_type: Optional[SymbolRef] = None
    interfaces: list[SymbolRef] = []
    members: list[SymbolRef] = []
    is_partial: bool = False
    declarations: int = 0


class ProjectNode(BaseModel):
    name: str
    path: Optional[str] = None
    documents: int = 0
    is_synthetic: bool = False


class DependencyEdge(BaseModel):
    src: str
    dst: str
    edge_type: str
    evidence: str = ""
    confidence: str = "exact"


class DependencyGraphOut(ToolEnvelope):
    projects: list[ProjectNode] = []
    edges: list[DependencyEdge] = []
    cycles: list[list[str]] = []


class DeadCodeOut(ToolEnvelope):
    scope: str = ""
    candidates_considered: int = 0
    unreferenced: list[SymbolRef] = []
    exclusions_applied: list[str] = []


class ReadFileOut(ToolEnvelope):
    file: str = ""
    rel_path: str = ""
    line_start: int = 0
    line_end: int = 0
    total_lines: int = 0
    lines: list[str] = []
    text: str = ""


class SearchHit(BaseModel):
    loc: Loc
    match: str


class SearchTextOut(ToolEnvelope):
    pattern: str = ""
    hits: list[SearchHit] = []
    files_searched: int = 0
    by_file: dict[str, int] = {}


class ControlBinding(BaseModel):
    control_id: Optional[str]
    tag: str = Field(description='As written, e.g. "democtrls:LookupControlEx".')
    resolved_type: Optional[str] = None
    type_resolution: str
    in_data_bound_template: bool = Field(
        default=False,
        description=(
            "True = the control sits inside ItemTemplate/EditItemTemplate/etc. "
            "ASP.NET does NOT generate a page-level field for these at runtime, "
            "but this index synthesises one anyway."
        ),
    )
    markup_loc: Loc
    type_decl: Optional[SymbolRef] = None
    codebehind_field: Optional[SymbolRef] = None
    codebehind_uses: list[Loc] = []
    server_handlers: list[SymbolRef] = []
    client_handlers: list[SymbolRef] = []
    unresolved_handlers: list[str] = Field(
        default_factory=list,
        description="Inline JS kept verbatim, never turned into a guessed name.",
    )


class GetControlBindingOut(ToolEnvelope):
    page: Optional[SymbolRef] = None
    inherits: Optional[str] = None
    codebehind: Optional[Loc] = None
    master_page: Optional[Loc] = None
    is_web_application_project: bool = False
    controls: list[ControlBinding] = []


class Asset(BaseModel):
    kind: str
    raw: str
    resolved_rel: Optional[str] = None
    confidence: str
    line: int


class GetPageAssetsOut(ToolEnvelope):
    page: Optional[SymbolRef] = None
    master_page: Optional[Loc] = None
    codebehind: Optional[Loc] = None
    user_controls: list[Loc] = []
    scripts: list[Asset] = []
    styles: list[Asset] = []
    imports: list[str] = []
    server_handlers: list[SymbolRef] = []
    client_handlers: list[SymbolRef] = []


class TwinShape(BaseModel):
    """Public properties/fields compared by name (case-insensitive, as JSON binds)."""
    score: float = Field(description="Shared members / all members, 0..1.")
    identical: bool
    common: int
    only_in_source: list[str] = []
    only_in_twin: list[str] = []
    type_differences: list[str] = Field(
        default_factory=list, description="'Name: sourceType vs twinType' for shared members.")
    nested: list[str] = Field(
        default_factory=list,
        description="'Name: sourceType ~ twinType': different types that are themselves twins (compatible).")


class MigrationTwin(BaseModel):
    symbol: SymbolRef
    match_kind: Literal[
        "same_fqn", "same_container_and_name", "same_name_only", "same_name_different_kind",
        "api_contract", "nested_contract",
    ]
    evidence: list[str] = Field(
        default_factory=list,
        description=(
            "Compiler-index evidence: 'api_contract' = this type is the request body at one "
            "end of an HTTP call and the twin at the other end (route_exact); 'same_name' = "
            "same simple name. Empty for graph (name-only) answers."
        ),
    )
    routes: list[str] = []
    shape: Optional[TwinShape] = None


class FindMigrationTwinOut(ToolEnvelope):
    query: str = ""
    from_side: Optional[Side] = None
    source_symbols: list[SymbolRef] = []
    twins: list[MigrationTwin] = []


class RoslynHostStatus(BaseModel):
    side: str
    state: str
    projects_loaded: int = 0
    projects_total: int = 0
    website_documents: int = 0
    elapsed_ms: int = 0
    failure_count: int = 0
    failures: list[str] = []
    solution: Optional[str] = None
    error: Optional[str] = None


class IndexStatusOut(ToolEnvelope):
    built_at: str = ""
    elapsed_s: str = ""
    files_indexed: int = 0
    files_skipped: dict[str, int] = {}
    lines_indexed: int = 0
    symbols: int = 0
    refs: int = 0
    edges: int = 0
    controls: int = 0
    pages: int = 0
    refs_by_resolution: dict[str, int] = {}
    parse_errors: list[str] = []
    parse_error_count: int = 0
    fts5: bool = True
    designer_files: int = 0
    designer_fields: int = 0
    designer_collisions: int = 0
    designer_template_fields: int = 0
    roslyn: dict[str, RoslynHostStatus] = {}
    # Where this index came from and where it is being served, so a mismatch is
    # visible rather than showing up as inexplicably empty answers.
    deployment: dict[str, str] = {}
    case_resolved_count: int = 0
    compiler_index: dict = Field(
        default_factory=dict,
        description=(
            "The persisted compiler index: whether it was built, per-side counts, and every "
            "project that compiled with binding errors."
        ),
    )
    projects: list[ProjectCompilation] = Field(
        default_factory=list,
        description="Per-project compile/build state, when index_status is called with 'project'.",
    )
    project_diagnostics: list[dict] = []
    not_indexed: list[str] = []
    known_limitations: list[str] = []


class ApiCallHit(BaseModel):
    verb: str
    route: Optional[str] = None
    url: str = Field(description="The URL as written at the call site; {} = computed at run time.")
    match: str = Field(description="route_exact | route_partial | route_ambiguous | unmatched")
    handler: Optional[SymbolRef] = None
    endpoint_side: Optional[Side] = None
    call_site: Loc
    chain: list[str] = Field(
        default_factory=list,
        description="Members from the starting point down to the one making the call.")
    via_dispatch: bool = False


class FindApiCallsOut(ToolEnvelope):
    start: str = ""
    members_walked: int = 0
    endpoints_reached: int = 0
    calls: list[ApiCallHit] = []
    unmatched_calls: list[ApiCallHit] = Field(
        default_factory=list,
        description=(
            "HTTP calls reached whose URL matches no endpoint: third-party services, or a "
            "route the API does not declare."
        ),
    )


CallNode.model_rebuild()
