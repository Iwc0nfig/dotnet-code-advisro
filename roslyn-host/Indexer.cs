using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Text;

namespace DotnetCodeAdvisor.RoslynHost;

/// <summary>What the Python side asks the index pass to load.  Written by index/roslyn_index.py.</summary>
public sealed class IndexSpec
{
    public string Side { get; set; } = "";
    public string Solution { get; set; } = "";
    public string WorkspacesRoot { get; set; } = "";
    public string OutDir { get; set; } = "";
    public Dictionary<string, string> Properties { get; set; } = new();
    public List<ExactWebsite> Websites { get; set; } = new();
    /// <summary>Roots of generated markup code; files under them are flagged generated.</summary>
    public List<string> MarkupDirs { get; set; } = new();
    /// <summary>
    /// Projects of the OTHER side that a project of this side references (e.g.
    /// the netstandard Demo.DocuSign used by Modern.Sign).  Loaded and
    /// compiled so references into them resolve, but not emitted: their own
    /// side's pass owns their declarations.
    /// </summary>
    public List<string> BorrowedProjects { get; set; } = new();
    /// <summary>ProjectReferences whose path does not exist, resolved by the Python side.</summary>
    public List<ReferenceRepair> ReferenceRepairs { get; set; } = new();
}

public sealed class ReferenceRepair
{
    public string Project { get; set; } = "";
    public string Missing { get; set; } = "";
    public string Target { get; set; } = "";
}

/// <summary>
/// The offline index pass (plan Phase 2): load every project of one side,
/// graft the Website Projects, substitute binary references to our own code,
/// compile everything, and write what the compiler resolved -- every
/// declaration, every reference, every inheritance/override edge -- as JSONL
/// for the Python loader.
///
///   usage: RoslynHost --index &lt;spec.json&gt;
///
/// Symbol identity is "declaring project | DocumentationCommentId" for source
/// symbols and "asm:Assembly | DocumentationCommentId" for metadata.  The
/// doc-id alone is not unique here: several projects build the same assembly
/// name and declare the same types.
///
/// Output rows are JSON arrays (not objects) to keep large reference sets small;
/// the column order is fixed in Columns below and mirrored in roslyn_index.py.
/// </summary>
public static class Indexer
{
    public static class Columns
    {
        public const string Symbols = "key,doc_id,project,kind,name,container_key,fqn,signature,accessibility,modifiers,path,line,col,end_line,is_generated,start_line,type,json_name";
        public const string Refs = "path,line,col,target_key,target_doc_id,target_in_source,target_assembly,ref_kind,enclosing_key,candidate_reason,project,is_generated";
        public const string Edges = "src_key,edge_type,dst_key";
        public const string Files = "project,path,generated_kind";
        public const string Endpoints = "project,verb,route,prefix_complete,handler_key,handler_kind,style,path,line,col,body_type_key";
        public const string Calls = "path,line,col,verb,url,enclosing_key,callee_key,project,is_generated,url_line,url_col,url_end_line,url_end_col,body_type_key";
    }

    private static readonly SymbolDisplayFormat FqnFormat = new(
        globalNamespaceStyle: SymbolDisplayGlobalNamespaceStyle.Omitted,
        typeQualificationStyle: SymbolDisplayTypeQualificationStyle.NameAndContainingTypesAndNamespaces,
        genericsOptions: SymbolDisplayGenericsOptions.IncludeTypeParameters,
        memberOptions: SymbolDisplayMemberOptions.IncludeContainingType);

    private static readonly JsonSerializerOptions SpecJson = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
    };

    public static async Task<int> RunAsync(WorkspaceHost host, string specPath)
    {
        var spec = JsonSerializer.Deserialize<IndexSpec>(File.ReadAllText(specPath), SpecJson)
                   ?? throw new InvalidOperationException("empty spec");
        Directory.CreateDirectory(spec.OutDir);
        var total = Stopwatch.StartNew();
        void Log(string m) => Console.Error.WriteLine($"[index {spec.Side} {total.Elapsed:mm\\:ss}] {m}");

        await host.LoadAsync(spec.Solution, null, spec.Side,
            (l, t, n) => { if (l % 25 == 0 || l == t) Log($"load {l}/{t} {n}"); },
            CancellationToken.None, spec.Properties).ConfigureAwait(false);
        var sln = host.Solution;
        Log($"loaded {sln.ProjectIds.Count} projects; {host.Failures.Count} load failures, "
            + $"{host.Warnings.Count} warnings; razor in {host.RazorProjects}");

        var root = Norm(spec.WorkspacesRoot);
        var keys = new Dictionary<ProjectId, string>();
        // A multi-targeted .csproj loads once per framework ("X(net462)",
        // "X(net472)"); each compilation gets its own key.
        var perPath = sln.Projects.Where(p => p.FilePath is not null)
                         .GroupBy(p => Norm(p.FilePath!), StringComparer.OrdinalIgnoreCase)
                         .ToDictionary(g => g.Key, g => g.Count(), StringComparer.OrdinalIgnoreCase);
        foreach (var p in sln.Projects)
        {
            if (p.FilePath is null) { keys[p.Id] = p.Name; continue; }
            var k = Rel(p.FilePath, root);
            if (perPath[Norm(p.FilePath)] > 1) k += "#" + FrameworkOf(p.Name);
            keys[p.Id] = k;
        }
        var borrowedPaths = new HashSet<string>(spec.BorrowedProjects.Select(Norm), StringComparer.OrdinalIgnoreCase);
        var borrowed = new HashSet<ProjectId>(sln.Projects
            .Where(p => p.FilePath is not null && borrowedPaths.Contains(Norm(p.FilePath)))
            .Select(p => p.Id));
        foreach (var w in spec.Websites)
        {
            sln = WebsiteProjectBuilder.GraftExact(sln, w, out var id);
            keys[id] = Rel(w.Root, root) + "#website";
            Log($"grafted {w.ProjectName}: {w.SourceFiles.Count} sources, {w.GeneratedFiles.Count} generated, "
                + $"{w.FallbackDesigners.Count} fallback designers, {w.References.Count} references");
        }

        var subs = new List<Substitution.Record>();
        sln = ApplyRepairs(sln, spec.ReferenceRepairs, subs);
        sln = Substitution.Apply(sln, subs);
        Log($"reference substitution: {string.Join(", ", subs.GroupBy(s => s.Action).Select(g => $"{g.Key}={g.Count()}"))}");

        var gates = await Gate.RunAsync(sln, _ => { }, CancellationToken.None, sampleErrors: int.MaxValue)
                              .ConfigureAwait(false);
        Log($"gate: {gates.Count(g => g.Clean)}/{gates.Count} projects clean, "
            + $"{gates.Sum(g => g.BindingErrors)} binding errors");

        // tree -> declaring project, for source-symbol keys (covers generated trees too).
        var treeOwner = new Dictionary<SyntaxTree, string>();
        foreach (var p in sln.Projects)
        {
            var comp = await p.GetCompilationAsync().ConfigureAwait(false);
            if (comp is null) continue;
            foreach (var t in comp.SyntaxTrees) treeOwner[t] = keys[p.Id];
        }

        var markupDirs = spec.MarkupDirs.Select(Norm).ToList();
        WriteMeta(spec, host,
                  gates.Where(g => g.FilePath is null || !borrowedPaths.Contains(Norm(g.FilePath))).ToList(),
                  subs, keys, sln, root);

        using var symW = Open(spec.OutDir, "symbols.jsonl");
        using var refW = Open(spec.OutDir, "refs.jsonl");
        using var edgeW = Open(spec.OutDir, "edges.jsonl");
        using var fileW = Open(spec.OutDir, "files.jsonl");
        using var epW = Open(spec.OutDir, "endpoints.jsonl");
        using var callW = Open(spec.OutDir, "http_calls.jsonl");
        long nSym = 0, nRef = 0, nEdge = 0, nEp = 0, nCall = 0;
        var fallbackKeys = 0;

        var projects = sln.Projects.OrderBy(p => keys[p.Id], StringComparer.Ordinal).ToList();
        var pi = 0;
        foreach (var p in projects)
        {
            pi++;
            if (borrowed.Contains(p.Id)) continue;
            var pkey = keys[p.Id];
            var docs = new List<(Document Doc, string Kind)>();
            foreach (var d in p.Documents.OrderBy(d => d.FilePath ?? d.Name, StringComparer.Ordinal))
            {
                var fp = d.FilePath is null ? "" : Norm(d.FilePath);
                var kind = markupDirs.Any(m => fp.StartsWith(m + "/", StringComparison.OrdinalIgnoreCase))
                    ? "aspnet" : fp.EndsWith(".synth.designer.cs", StringComparison.OrdinalIgnoreCase)
                    ? "synthesised" : "";
                docs.Add((d, kind));
            }
            foreach (var g in (await p.GetSourceGeneratedDocumentsAsync().ConfigureAwait(false))
                              .OrderBy(g => g.HintName, StringComparer.Ordinal))
                docs.Add((g, "source_generator"));

            var results = new DocOut[docs.Count];
            using var throttle = new SemaphoreSlim(Environment.ProcessorCount);
            var tasks = docs.Select(async (item, i) =>
            {
                await throttle.WaitAsync().ConfigureAwait(false);
                try
                {
                    results[i] = await WalkAsync(item.Doc, item.Kind, pkey, root, treeOwner)
                                        .ConfigureAwait(false);
                }
                catch (Exception ex)
                {
                    results[i] = new DocOut { Error = ex.GetType().Name + ": " + ex.Message };
                }
                finally { throttle.Release(); }
            }).ToList();
            await Task.WhenAll(tasks).ConfigureAwait(false);

            for (var i = 0; i < results.Length; i++)
            {
                var r = results[i];
                var path = docs[i].Doc.FilePath is null ? docs[i].Doc.Name : Rel(docs[i].Doc.FilePath!, root);
                Row(fileW, pkey, path, docs[i].Kind);
                foreach (var s in r.Syms) { symW.WriteLine(s); nSym++; }
                foreach (var s in r.Refs) { refW.WriteLine(s); nRef++; }
                foreach (var s in r.Edges) { edgeW.WriteLine(s); nEdge++; }
                foreach (var s in r.Endpoints) { epW.WriteLine(s); nEp++; }
                foreach (var s in r.Calls) { callW.WriteLine(s); nCall++; }
                fallbackKeys += r.FallbackKeys;
                if (r.Error is not null) Log($"  ! {path}: {r.Error}");
            }
            if (pi % 20 == 0 || pi == projects.Count)
                Log($"walked {pi}/{projects.Count} projects: {nSym:N0} symbols, {nRef:N0} refs, {nEdge:N0} edges");
        }

        var proc = Process.GetCurrentProcess();
        proc.Refresh();
        File.WriteAllText(Path.Combine(spec.OutDir, "done.json"), JsonSerializer.Serialize(new
        {
            side = spec.Side,
            symbols = nSym,
            refs = nRef,
            edges = nEdge,
            endpoints = nEp,
            http_calls = nCall,
            fallback_keys = fallbackKeys,
            elapsed_s = total.Elapsed.TotalSeconds,
            peak_mb = proc.PeakWorkingSet64 / (1024 * 1024),
            columns = new
            {
                symbols = Columns.Symbols, refs = Columns.Refs, edges = Columns.Edges, files = Columns.Files,
                endpoints = Columns.Endpoints, http_calls = Columns.Calls,
            },
        }));
        Log($"done: {nSym:N0} symbols, {nRef:N0} refs, {nEdge:N0} edges, "
            + $"peak {proc.PeakWorkingSet64 / (1024 * 1024)} MB");
        return 0;
    }

    // ------------------------------------------------------------------ walk

    private sealed class DocOut
    {
        public List<string> Syms { get; } = new();
        public List<string> Refs { get; } = new();
        public List<string> Edges { get; } = new();
        public List<string> Endpoints { get; } = new();
        public List<string> Calls { get; } = new();
        public int FallbackKeys;
        public string? Error;
    }

    private static async Task<DocOut> WalkAsync(Document doc, string genKind, string pkey, string root,
                                                 Dictionary<SyntaxTree, string> treeOwner)
    {
        var o = new DocOut();
        var model = await doc.GetSemanticModelAsync().ConfigureAwait(false);
        var rootNode = await doc.GetSyntaxRootAsync().ConfigureAwait(false);
        if (model is null || rootNode is null) return o;
        var tree = rootNode.SyntaxTree;
        var generated = genKind.Length > 0;
        var colMap = genKind == "aspnet" ? LoadColumnMap(doc.FilePath) : null;
        var enclosing = new Dictionary<SyntaxNode, string?>();

        string? Key(ISymbol s)
        {
            var id = DocumentationCommentId.CreateDeclarationId(s);
            if (id is null) return null;
            var loc = s.Locations.FirstOrDefault(l => l.IsInSource);
            if (loc?.SourceTree is not null)
            {
                if (treeOwner.TryGetValue(loc.SourceTree, out var owner)) return owner + "|" + id;
                o.FallbackKeys++;
                return "src:" + (s.ContainingAssembly?.Name ?? "?") + "|" + id;
            }
            return "asm:" + (s.ContainingAssembly?.Name ?? "?") + "|" + id;
        }

        (string Path, int Line, int Col, bool Hidden) Where(TextSpan span)
        {
            var hidden = tree.GetLineVisibility(span.Start) == LineVisibility.Hidden;
            var m = tree.GetMappedLineSpan(span);
            var path = m.HasMappedPath ? m.Path : (tree.FilePath ?? doc.Name);
            var line = m.StartLinePosition.Line + 1;
            var col = m.StartLinePosition.Character + 1;
            if (m.HasMappedPath && colMap is not null)
            {
                var genLine = tree.GetLineSpan(span).StartLinePosition.Line + 1;
                if (colMap.TryGetValue(genLine, out var e) && e.GenCol > 0)
                    col = col - e.GenCol + e.SrcCol;
            }
            return (Rel(path, root), line, col, hidden);
        }

        string? Enclosing(SyntaxNode node)
        {
            for (var n = node.Parent; n is not null; n = n.Parent)
            {
                if (!IsDeclaration(n)) continue;
                if (enclosing.TryGetValue(n, out var k)) return k;
                var s = DeclaredSymbol(model, n);
                k = s is null ? null : Key(s);
                enclosing[n] = k;
                return k;
            }
            return null;
        }

        void Edge(string? src, string type, ISymbol? dst)
        {
            if (src is null || dst is null) return;
            var dk = Key(dst.OriginalDefinition);
            if (dk is not null) o.Edges.Add(J(src, type, dk));
        }

        void Ref(SyntaxNode node, string kind)
        {
            var info = model.GetSymbolInfo(node);
            var list = new List<(ISymbol S, string? Reason)>();
            if (info.Symbol is not null) list.Add((info.Symbol, null));
            else foreach (var c in info.CandidateSymbols) list.Add((c, info.CandidateReason.ToString()));
            if (list.Count == 0) return;
            var w = Where(node.Span);
            if (w.Hidden && generated) return;
            foreach (var (raw, reason) in list)
            {
                var s = Normalize(raw);
                if (s is null) continue;
                var key = Key(s);
                if (key is null) continue;
                var inSource = s.Locations.Any(l => l.IsInSource);
                var k = kind == "name" ? RefKind(node, s) : kind;
                o.Refs.Add(J(w.Path, w.Line, w.Col, key, DocumentationCommentId.CreateDeclarationId(s),
                             inSource ? 1 : 0, s.ContainingAssembly?.Name, k, Enclosing(node),
                             reason, pkey, generated ? 1 : 0));
            }
        }

        string? TypeKey(ITypeSymbol? t) => t is null ? null : Normalize(t) is { } n ? Key(n) : null;

        void EmitEndpoint(HttpLinks.Endpoint ep)
        {
            var w = Where(ep.Node.Span);
            if (w.Hidden && generated) return;
            var hk = ep.Handler is null ? Enclosing(ep.Node)
                   : Normalize(ep.Handler) is { } hs ? Key(hs) : null;
            o.Endpoints.Add(J(pkey, ep.Verb, ep.Route, ep.PrefixComplete ? 1 : 0, hk, ep.HandlerKind,
                              ep.Style, w.Path, w.Line, w.Col, TypeKey(ep.Body)));
        }

        void EmitCall(HttpLinks.Call c)
        {
            var w = Where(c.Node.Span);
            if (w.Hidden && generated) return;
            var u = Where(c.UrlNode.Span);
            var ue = tree.GetMappedLineSpan(c.UrlNode.Span).EndLinePosition;
            var ck = c.Callee is null ? null : Normalize(c.Callee) is { } cs ? Key(cs) : null;
            o.Calls.Add(J(w.Path, w.Line, w.Col, c.Verb, c.Url, Enclosing(c.Node), ck, pkey,
                          generated ? 1 : 0, u.Line, u.Col, ue.Line + 1, ue.Character + 1, TypeKey(c.Body)));
        }

        foreach (var node in rootNode.DescendantNodes())
        {
            if (node is InvocationExpressionSyntax inv && HttpLinks.MinimalEndpoint(inv, model) is { } mep)
                EmitEndpoint(mep);
            if (node is InvocationExpressionSyntax or ObjectCreationExpressionSyntax
                && HttpLinks.HttpCall((ExpressionSyntax)node, model) is { } hc)
                EmitCall(hc);

            switch (node)
            {
                case SimpleNameSyntax:
                    Ref(node, "name");
                    break;
                case ObjectCreationExpressionSyntax:
                case ImplicitObjectCreationExpressionSyntax:
                    Ref(node, "new");
                    break;
                case ConstructorInitializerSyntax:
                    Ref(node, "base_ctor");
                    break;
            }

            if (!IsDeclaration(node)) continue;
            var sym = DeclaredSymbol(model, node);
            if (sym is null) continue;
            var key = Key(sym);
            if (key is null) continue;
            enclosing[node] = key;
            var nameLoc = sym.Locations.FirstOrDefault(l => l.SourceTree == tree && node.Span.Contains(l.SourceSpan));
            var at = Where(nameLoc?.SourceSpan ?? node.Span);
            var mapped = tree.GetMappedLineSpan(node.Span);
            var end = mapped.EndLinePosition.Line + 1;
            // The declaration's first line, attributes and modifiers included:
            // a position anywhere in that header means this declaration.
            var start = mapped.StartLinePosition.Line + 1;
            o.Syms.Add(J(key, DocumentationCommentId.CreateDeclarationId(sym), pkey, Kind(sym),
                         sym is IMethodSymbol { MethodKind: MethodKind.Constructor or MethodKind.StaticConstructor }
                             ? sym.ContainingType.Name : sym.Name,
                         sym.ContainingType is not null ? Key(sym.ContainingType)
                             : "ns:" + sym.ContainingNamespace?.ToDisplayString(FqnFormat),
                         sym.ToDisplayString(FqnFormat),
                         sym.ToDisplayString(SymbolDisplayFormat.CSharpErrorMessageFormat),
                         sym.DeclaredAccessibility.ToString().ToLowerInvariant(),
                         Modifiers(sym), at.Path, at.Line, at.Col, end, generated ? 1 : 0,
                         Math.Min(start, at.Line), MemberType(sym), JsonName(sym)));

            if (sym is IMethodSymbol ms)
                foreach (var ep in HttpLinks.ControllerEndpoints(ms, node))
                    EmitEndpoint(ep);

            switch (sym)
            {
                case INamedTypeSymbol t:
                    if (t.BaseType is not null && t.BaseType.SpecialType == SpecialType.None
                        && t.TypeKind is TypeKind.Class)
                        Edge(key, "inherits", t.BaseType);
                    foreach (var i in t.Interfaces) Edge(key, "implements", i);
                    foreach (var iface in t.AllInterfaces)
                        foreach (var member in iface.GetMembers())
                        {
                            var impl = t.FindImplementationForInterfaceMember(member);
                            if (impl is not null && SymbolEqualityComparer.Default.Equals(impl.ContainingType, t))
                            {
                                var ik = Key(impl);
                                if (ik is not null) Edge(ik, "implements_member", member);
                            }
                        }
                    break;
                case IMethodSymbol m when m.OverriddenMethod is not null:
                    Edge(key, "overrides", m.OverriddenMethod);
                    break;
                case IPropertySymbol pr when pr.OverriddenProperty is not null:
                    Edge(key, "overrides", pr.OverriddenProperty);
                    break;
                case IEventSymbol ev when ev.OverriddenEvent is not null:
                    Edge(key, "overrides", ev.OverriddenEvent);
                    break;
            }
        }
        return o;
    }

    private static readonly SymbolDisplayFormat TypeFormat = SymbolDisplayFormat.MinimallyQualifiedFormat;

    /// <summary>A member's type as written without namespaces ("List&lt;AuditRecord&gt;", "int?"): property, field, event, method return.</summary>
    private static string? MemberType(ISymbol s) => s switch
    {
        IPropertySymbol p => p.Type.ToDisplayString(TypeFormat),
        IFieldSymbol f => f.Type.ToDisplayString(TypeFormat),
        IEventSymbol e => e.Type.ToDisplayString(TypeFormat),
        IMethodSymbol m => m.ReturnType.ToDisplayString(TypeFormat),
        _ => null,
    };

    /// <summary>
    /// The name a property/field has on the wire when an attribute renames it:
    /// [JsonProperty("x")] / [JsonProperty(PropertyName = "x")] (Newtonsoft),
    /// [JsonPropertyName("x")] (System.Text.Json), [DataMember(Name = "x")].
    /// </summary>
    private static string? JsonName(ISymbol s)
    {
        if (s is not (IPropertySymbol or IFieldSymbol)) return null;
        foreach (var a in s.GetAttributes())
        {
            var n = a.AttributeClass?.Name;
            if (n is "JsonPropertyAttribute" or "JsonPropertyNameAttribute")
            {
                if (a.ConstructorArguments.FirstOrDefault(c => c.Value is string).Value is string ctor) return ctor;
                foreach (var na in a.NamedArguments)
                    if (na.Key == "PropertyName" && na.Value.Value is string pn) return pn;
            }
            else if (n == "DataMemberAttribute")
            {
                foreach (var na in a.NamedArguments)
                    if (na.Key == "Name" && na.Value.Value is string dn) return dn;
            }
        }
        return null;
    }

    /// <summary>"RedisSessionStateProvider(net462)" -> "net462".</summary>
    private static string FrameworkOf(string projectName)
    {
        var open = projectName.LastIndexOf('(');
        return open >= 0 && projectName.EndsWith(")")
            ? projectName.Substring(open + 1, projectName.Length - open - 2)
            : projectName;
    }

    /// <summary>
    /// Add the ProjectReferences whose path in the .csproj does not exist but
    /// whose target was found unambiguously elsewhere in the workspace (another
    /// machine's folder layout baked into the path).  Recorded like a
    /// substitution; the .csproj itself is not touched.
    /// </summary>
    private static Solution ApplyRepairs(Solution sln, List<ReferenceRepair> repairs, List<Substitution.Record> log)
    {
        ProjectId? Find(string path)
        {
            var n = Norm(path);
            return sln.Projects.Where(p => p.FilePath is not null
                                           && string.Equals(Norm(p.FilePath), n, StringComparison.OrdinalIgnoreCase))
                      .OrderBy(p => p.Name, StringComparer.Ordinal)
                      .Select(p => p.Id).FirstOrDefault();
        }
        foreach (var r in repairs)
        {
            var rec = new Substitution.Record { Project = r.Project, Reference = r.Missing, Target = r.Target, Candidates = 1 };
            var from = Find(r.Project);
            var to = Find(r.Target);
            if (from is null || to is null) rec.Action = "repair_not_loaded";
            else if (sln.GetProject(from)!.ProjectReferences.Any(x => x.ProjectId == to)) rec.Action = "repair_present";
            else if (sln.GetProjectDependencyGraph().GetProjectsThatThisProjectTransitivelyDependsOn(to).Contains(from))
                rec.Action = "repair_cycle";
            else
            {
                try
                {
                    sln = Substitution.DropDangling(sln, from);
                    sln = sln.AddProjectReference(from, new ProjectReference(to));
                    rec.Action = "repaired_reference";
                }
                catch (Exception ex)
                {
                    rec.Action = "repair_failed: " + ex.GetType().Name + ": " + ex.Message;
                }
            }
            log.Add(rec);
        }
        return sln;
    }

    private static bool IsDeclaration(SyntaxNode n) => n switch
    {
        BaseTypeDeclarationSyntax or DelegateDeclarationSyntax or EnumMemberDeclarationSyntax
            or BaseMethodDeclarationSyntax or BasePropertyDeclarationSyntax => true,
        VariableDeclaratorSyntax { Parent.Parent: BaseFieldDeclarationSyntax } => true,
        // record R(string A, int B): each positional parameter declares property R.A / R.B
        ParameterSyntax { Parent.Parent: RecordDeclarationSyntax } => true,
        _ => false,
    };

    private static ISymbol? DeclaredSymbol(SemanticModel model, SyntaxNode n)
    {
        if (n is ParameterSyntax { Parent.Parent: RecordDeclarationSyntax } ps)
        {
            // The compiler synthesizes the property; report it, at the parameter.
            return model.GetDeclaredSymbol(ps) is IParameterSymbol { ContainingSymbol: IMethodSymbol ctor } p
                ? ctor.ContainingType.GetMembers(p.Name).OfType<IPropertySymbol>().FirstOrDefault()
                : null;
        }
        return model.GetDeclaredSymbol(n);
    }

    /// <summary>
    /// The symbol a use should be filed under: the unconstructed definition
    /// (List&lt;int&gt;.Add -> List&lt;T&gt;.Add), the extension method as declared, the
    /// alias target.  Locals, parameters, namespaces and the like are not
    /// indexed -- they cannot be referenced from another member.
    /// </summary>
    private static ISymbol? Normalize(ISymbol s)
    {
        if (s is IAliasSymbol a) s = a.Target;
        if (s is IMethodSymbol m && m.ReducedFrom is not null) s = m.ReducedFrom;
        switch (s.Kind)
        {
            case SymbolKind.Local:
            case SymbolKind.Parameter:
            case SymbolKind.RangeVariable:
            case SymbolKind.Label:
            case SymbolKind.TypeParameter:
            case SymbolKind.Discard:
            case SymbolKind.Namespace:
            case SymbolKind.Preprocessing:
            case SymbolKind.DynamicType:
            case SymbolKind.ArrayType:
            case SymbolKind.PointerType:
            case SymbolKind.ErrorType:
                return null;
        }
        if (s is IMethodSymbol { MethodKind: MethodKind.LocalFunction or MethodKind.AnonymousFunction }) return null;
        if (s is INamedTypeSymbol { IsAnonymousType: true } or INamedTypeSymbol { IsTupleType: true }) return null;
        return s.OriginalDefinition;
    }

    private static string RefKind(SyntaxNode node, ISymbol s)
    {
        if (node.Parent is AttributeSyntax) return "attribute";
        if (s is ITypeSymbol) return "type_use";
        var expr = node.Parent is MemberAccessExpressionSyntax ma && ma.Name == node ? (SyntaxNode)ma : node;
        if (expr.Parent is InvocationExpressionSyntax inv && inv.Expression == expr) return "call";
        if (s is IMethodSymbol) return "method_group";
        if (expr.Parent is AssignmentExpressionSyntax asg && asg.Left == expr) return "write";
        return "read";
    }

    private static string Kind(ISymbol s) => s switch
    {
        INamedTypeSymbol { IsRecord: true } => "record",
        INamedTypeSymbol { TypeKind: TypeKind.Interface } => "interface",
        INamedTypeSymbol { TypeKind: TypeKind.Struct } => "struct",
        INamedTypeSymbol { TypeKind: TypeKind.Enum } => "enum",
        INamedTypeSymbol { TypeKind: TypeKind.Delegate } => "delegate",
        INamedTypeSymbol => "class",
        IMethodSymbol { MethodKind: MethodKind.Constructor or MethodKind.StaticConstructor } => "constructor",
        IMethodSymbol => "method",
        IPropertySymbol { IsIndexer: true } => "indexer",
        IPropertySymbol => "property",
        IFieldSymbol => "field",
        IEventSymbol => "event",
        _ => s.Kind.ToString().ToLowerInvariant(),
    };

    private static string Modifiers(ISymbol s)
    {
        var m = new List<string>();
        if (s.IsStatic) m.Add("static");
        if (s.IsAbstract) m.Add("abstract");
        if (s.IsVirtual) m.Add("virtual");
        if (s.IsOverride) m.Add("override");
        if (s.IsSealed && s is not INamedTypeSymbol { TypeKind: TypeKind.Struct or TypeKind.Enum }) m.Add("sealed");
        if (s.IsExtern) m.Add("extern");
        if (s is IMethodSymbol { IsAsync: true }) m.Add("async");
        if (s is INamedTypeSymbol t && t.DeclaringSyntaxReferences.Length > 1) m.Add("partial");
        return string.Join(" ", m);
    }

    // ------------------------------------------------------------------ io

    private sealed class MapEntry
    {
        public int GenLine { get; set; }
        public int GenCol { get; set; }
        public int SrcLine { get; set; }
        public int SrcCol { get; set; }
    }

    private static Dictionary<int, MapEntry>? LoadColumnMap(string? generatedPath)
    {
        if (generatedPath is null || !generatedPath.EndsWith(".g.cs", StringComparison.OrdinalIgnoreCase))
            return null;
        var mapPath = generatedPath.Substring(0, generatedPath.Length - ".g.cs".Length) + ".map.json";
        if (!File.Exists(mapPath)) return null;
        var entries = JsonSerializer.Deserialize<List<MapEntry>>(File.ReadAllText(mapPath), SpecJson) ?? new();
        var d = new Dictionary<int, MapEntry>();
        foreach (var e in entries) d[e.GenLine] = e;
        return d;
    }

    private static void WriteMeta(IndexSpec spec, WorkspaceHost host, List<Gate.ProjectGate> gates,
                                  List<Substitution.Record> subs, Dictionary<ProjectId, string> keys,
                                  Solution sln, string root)
    {
        using (var w = Open(spec.OutDir, "projects.jsonl"))
        {
            foreach (var g in gates)
            {
                var proj = sln.Projects.First(p => p.Name == g.Name && (p.FilePath ?? "") == (g.FilePath ?? ""));
                var key = keys[proj.Id];
                var failures = proj.FilePath is null ? new List<string>()
                    : host.Failures.Where(f => f.IndexOf(proj.FilePath, StringComparison.OrdinalIgnoreCase) >= 0)
                                   .Distinct().ToList();
                Row(w, key, g.Name, g.AssemblyName, g.Documents, g.GeneratedDocuments,
                    g.MetadataReferences, proj.ProjectReferences.Count(), g.Errors, g.BindingErrors,
                    g.Warnings, g.Clean ? 1 : 0, g.Failure,
                    string.Join(",", g.TopErrors.Select(t => $"{t.Id}:{t.Count}")),
                    JsonSerializer.Serialize(failures), key.EndsWith("#website") ? 1 : 0);
            }
        }
        using (var w = Open(spec.OutDir, "diagnostics.jsonl"))
        {
            foreach (var g in gates)
            {
                var proj = sln.Projects.First(p => p.Name == g.Name && (p.FilePath ?? "") == (g.FilePath ?? ""));
                foreach (var e in g.SampleErrors)
                    Row(w, keys[proj.Id], e.Id, Gate.BindingErrorIds.Contains(e.Id) ? 1 : 0,
                        e.File is null ? null : Rel(e.File, root), e.Line, e.Message);
            }
        }
        using (var w = Open(spec.OutDir, "substitutions.jsonl"))
            foreach (var s in subs)
                Row(w, Rel(s.Project, root), Rel(s.Reference, root), s.Action,
                    s.Target is null ? null : Rel(s.Target, root), s.Candidates);
        using (var w = Open(spec.OutDir, "load_failures.jsonl"))
        {
            foreach (var f in host.Failures) Row(w, "failure", f);
            foreach (var f in host.Warnings) Row(w, "warning", f);
        }
    }

    private static StreamWriter Open(string dir, string name) =>
        new(Path.Combine(dir, name), false, new UTF8Encoding(false), 1 << 20);

    private static void Row(StreamWriter w, params object?[] cols) => w.WriteLine(J(cols));

    private static string J(params object?[] cols) => JsonSerializer.Serialize(cols);

    private static string Norm(string p) => Path.GetFullPath(p).Replace('\\', '/').TrimEnd('/');

    /// <summary>Workspace-relative with forward slashes when under the root, else absolute.</summary>
    private static string Rel(string p, string root)
    {
        string full;
        try { full = Norm(p); } catch { return p.Replace('\\', '/'); }
        return full.StartsWith(root + "/", StringComparison.OrdinalIgnoreCase)
            ? full.Substring(root.Length + 1)
            : full;
    }
}
