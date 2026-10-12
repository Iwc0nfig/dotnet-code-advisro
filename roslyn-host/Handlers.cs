using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.FindSymbols;
using Microsoft.CodeAnalysis.Text;

namespace DotnetCodeAdvisor.RoslynHost;

/// <summary>
/// Query handlers.  No invention allowed anywhere in this file: when a symbol
/// cannot be found at a position the answer is an error, never a name-based
/// guess, and every truncated result says so.
/// </summary>
public sealed class Handlers
{
    private readonly WorkspaceHost _host;

    public Handlers(WorkspaceHost host) => _host = host;

    // --- position plumbing -------------------------------------------------

    private async Task<(Document doc, int position)> LocateAsync(string file, int line, int col)
    {
        var doc = _host.FindDocuments(file).FirstOrDefault()
                  ?? throw new RpcException("file_not_in_workspace",
                        $"'{file}' is not a document in any loaded project or in the " +
                        "grafted website project.");
        var text = await doc.GetTextAsync().ConfigureAwait(false);
        if (line < 1 || line > text.Lines.Count)
            throw new RpcException("invalid_params",
                $"line {line} out of range (file has {text.Lines.Count} lines)");
        var tl = text.Lines[line - 1];
        var offset = Math.Max(0, Math.Min(col - 1, tl.SpanIncludingLineBreak.Length - 1));
        return (doc, tl.Start + offset);
    }

    private async Task<ISymbol> SymbolAtAsync(string file, int line, int col)
    {
        var (doc, position) = await LocateAsync(file, line, col).ConfigureAwait(false);
        var root = await doc.GetSyntaxRootAsync().ConfigureAwait(false);
        var model = await doc.GetSemanticModelAsync().ConfigureAwait(false);
        if (root is null || model is null)
            throw new RpcException("internal", "no syntax root or semantic model");

        var token = root.FindToken(position);

        // Walk the full parent chain rather than stopping at the first
        // non-expression node.  A caller frequently points at the first line of
        // a declaration, which for an attributed type is the '[' of the
        // attribute list -- tree-sitter reports the declaration as starting
        // there.  Refusing that position would make go_to_definition and
        // find_references unusable on every attributed type in the codebase.
        for (var node = token.Parent; node is not null; node = node.Parent)
        {
            var info = model.GetSymbolInfo(node);
            if (info.Symbol is not null) return info.Symbol;
            if (info.CandidateSymbols.Length == 1) return info.CandidateSymbols[0];

            var declared = model.GetDeclaredSymbol(node);
            if (declared is not null) return declared;

            if (node is CompilationUnitSyntax) break;
        }
        throw new RpcException("no_symbol_at_position",
            $"no symbol bound at {file}:{line}:{col} (token '{token.ValueText}')");
    }

    // --- DTOs --------------------------------------------------------------

    private static object LocationDto(Location loc, string? snippet = null)
    {
        var span = loc.GetLineSpan();
        return new
        {
            file = span.Path?.Replace('\\', '/'),
            line_start = span.StartLinePosition.Line + 1,
            line_end = span.EndLinePosition.Line + 1,
            col_start = span.StartLinePosition.Character + 1,
            snippet,
        };
    }

    private static string? SnippetOf(Location loc)
    {
        if (!loc.IsInSource || loc.SourceTree is null) return null;
        var text = loc.SourceTree.GetText();
        var line = text.Lines.GetLineFromPosition(loc.SourceSpan.Start);
        var s = line.ToString().Trim();
        return s.Length > 400 ? s.Substring(0, 400) : s;
    }

    private static object SymbolDto(ISymbol s)
    {
        var loc = s.Locations.FirstOrDefault(l => l.IsInSource) ?? s.Locations.FirstOrDefault();
        return new
        {
            name = s.Name,
            kind = s.Kind.ToString().ToLowerInvariant(),
            fqn = s.ToDisplayString(SymbolDisplayFormat.FullyQualifiedFormat)
                   .Replace("global::", ""),
            container = s.ContainingSymbol?.ToDisplayString(
                SymbolDisplayFormat.FullyQualifiedFormat).Replace("global::", ""),
            signature = s.ToDisplayString(),
            accessibility = s.DeclaredAccessibility.ToString().ToLowerInvariant(),
            is_static = s.IsStatic,
            is_abstract = s.IsAbstract,
            assembly = s.ContainingAssembly?.Name,
            project = null as string,
            loc = loc is null ? null : LocationDto(loc, loc.IsInSource ? SnippetOf(loc) : null),
            partial_declarations = s.Locations.Count(l => l.IsInSource),
        };
    }

    // --- methods -----------------------------------------------------------

    public async Task<object> SymbolAt(string file, int line, int col)
    {
        var s = await SymbolAtAsync(file, line, col).ConfigureAwait(false);
        return SymbolDto(s);
    }

    public async Task<object> FindSymbol(string query, int max)
    {
        var sln = _host.Solution;
        var found = await SymbolFinder
            .FindSourceDeclarationsAsync(sln, name => Matches(name, query), SymbolFilter.All)
            .ConfigureAwait(false);
        var list = found.Take(max).Select(SymbolDto).ToList();
        return new
        {
            symbols = list,
            total_available = found.Count(),
            truncated = found.Count() > max,
        };
    }

    private static bool Matches(string name, string query) =>
        name.Equals(query, StringComparison.OrdinalIgnoreCase) ||
        name.IndexOf(query, StringComparison.OrdinalIgnoreCase) >= 0;

    public async Task<object> GoToDefinition(string file, int line, int col)
    {
        var s = await SymbolAtAsync(file, line, col).ConfigureAwait(false);
        var defs = s.Locations.Where(l => l.IsInSource)
                    .Select(l => LocationDto(l, SnippetOf(l))).ToList();
        return new
        {
            target = SymbolDto(s),
            definitions = defs,
            partial_declarations = defs.Count,
            // Metadata-only symbols have no source location; say so rather than
            // returning an empty list that reads as "not found".
            in_metadata = defs.Count == 0,
        };
    }

    public async Task<object> FindReferences(string file, int line, int col, int max)
    {
        var s = await SymbolAtAsync(file, line, col).ConfigureAwait(false);
        var found = await SymbolFinder.FindReferencesAsync(s, _host.Solution)
                                      .ConfigureAwait(false);
        var locs = new List<object>();
        var byFile = new Dictionary<string, int>();
        var total = 0;
        foreach (var r in found)
        {
            foreach (var l in r.Locations)
            {
                total++;
                var span = l.Location.GetLineSpan();
                var path = span.Path?.Replace('\\', '/') ?? "";
                byFile[path] = byFile.TryGetValue(path, out var c) ? c + 1 : 1;
                if (locs.Count < max)
                    locs.Add(LocationDto(l.Location, SnippetOf(l.Location)));
            }
        }
        return new
        {
            target = SymbolDto(s),
            references = locs,
            by_file = byFile,
            total_available = total,
            truncated = total > max,
        };
    }

    public async Task<object> FindCallers(string file, int line, int col, int depth, int max)
    {
        var s = await SymbolAtAsync(file, line, col).ConfigureAwait(false);
        depth = Math.Max(1, Math.Min(depth, 5));
        var visited = new HashSet<ISymbol>(SymbolEqualityComparer.Default);
        var node = await BuildCallTree(s, depth, visited, max).ConfigureAwait(false);
        return node;
    }

    private async Task<object> BuildCallTree(ISymbol symbol, int depth,
                                             HashSet<ISymbol> path, int max)
    {
        if (!path.Add(symbol))
            return new { symbol = SymbolDto(symbol), cycle = true, callers = Array.Empty<object>() };

        var callers = new List<object>();
        if (depth > 0)
        {
            var found = await SymbolFinder.FindCallersAsync(symbol, _host.Solution)
                                          .ConfigureAwait(false);
            foreach (var c in found.Take(max))
            {
                callers.Add(await BuildCallTree(c.CallingSymbol, depth - 1, path, max)
                                  .ConfigureAwait(false));
            }
        }
        path.Remove(symbol);
        return new { symbol = SymbolDto(symbol), cycle = false, callers };
    }

    public async Task<object> FindImplementations(string file, int line, int col, int max)
    {
        var s = await SymbolAtAsync(file, line, col).ConfigureAwait(false);
        var sln = _host.Solution;
        var impls = new List<object>();
        var derived = new List<object>();

        if (s is INamedTypeSymbol nt)
        {
            if (nt.TypeKind == TypeKind.Interface)
            {
                foreach (var i in await SymbolFinder.FindImplementationsAsync(nt, sln)
                                                    .ConfigureAwait(false))
                    impls.Add(SymbolDto(i));
            }
            foreach (var d in await SymbolFinder.FindDerivedClassesAsync(nt, sln)
                                                .ConfigureAwait(false))
                derived.Add(SymbolDto(d));
        }
        else
        {
            foreach (var i in await SymbolFinder.FindImplementationsAsync(s, sln)
                                                .ConfigureAwait(false))
                impls.Add(SymbolDto(i));
            foreach (var o in await SymbolFinder.FindOverridesAsync(s, sln)
                                                .ConfigureAwait(false))
                derived.Add(SymbolDto(o));
        }

        return new
        {
            target = SymbolDto(s),
            implementations = impls.Take(max).ToList(),
            derived = derived.Take(max).ToList(),
            truncated = impls.Count > max || derived.Count > max,
        };
    }

    public async Task<object> TypeInfo(string file, int line, int col)
    {
        var s = await SymbolAtAsync(file, line, col).ConfigureAwait(false);
        var type = s as INamedTypeSymbol
                   ?? (s as IMethodSymbol)?.ContainingType
                   ?? (s as IPropertySymbol)?.ContainingType
                   ?? (s as IFieldSymbol)?.ContainingType
                   ?? s.ContainingType;
        if (type is null)
            throw new RpcException("no_symbol_at_position",
                $"symbol at {file}:{line}:{col} has no containing type");

        return new
        {
            target = SymbolDto(type),
            type_kind = type.TypeKind.ToString().ToLowerInvariant(),
            base_type = type.BaseType is null ? null : SymbolDto(type.BaseType),
            interfaces = type.AllInterfaces.Select(SymbolDto).ToList(),
            members = type.GetMembers()
                          .Where(m => !m.IsImplicitlyDeclared)
                          .Select(SymbolDto).ToList(),
            is_partial = type.Locations.Count(l => l.IsInSource) > 1,
            declarations = type.Locations.Count(l => l.IsInSource),
        };
    }

    public object ProjectGraph()
    {
        var sln = _host.Solution;
        var projects = sln.Projects.Select(p => new
        {
            name = p.Name,
            id = p.Id.Id.ToString(),
            path = p.FilePath?.Replace('\\', '/'),
            documents = p.DocumentIds.Count,
            language = p.Language,
            is_synthetic = p.Name == WebsiteProjectBuilder.ProjectName,
        }).ToList();

        var edges = new List<object>();
        foreach (var p in sln.Projects)
            foreach (var r in p.ProjectReferences)
            {
                var t = sln.GetProject(r.ProjectId);
                if (t is not null) edges.Add(new { from = p.Name, to = t.Name });
            }

        return new { projects, edges };
    }

    /// <summary>
    /// Scoped to one project or one file, never solution-wide: on a codebase
    /// this size the full sweep takes tens of minutes, and the WebForms
    /// reflection surface makes solution-wide results untrustworthy anyway.
    /// The exclusions applied are returned so the caller states the caveat
    /// instead of inventing one.
    /// </summary>
    public async Task<object> DeadCode(string? scope, int max)
    {
        if (string.IsNullOrWhiteSpace(scope))
            throw new RpcException("invalid_params",
                "dead_code requires 'scope': a project name or a file path. " +
                "Solution-wide sweeps are refused by design.");

        var sln = _host.Solution;
        var projects = sln.Projects.Where(p =>
            string.Equals(p.Name, scope, StringComparison.OrdinalIgnoreCase)).ToList();
        var documents = new List<Document>();
        if (projects.Count > 0)
            documents.AddRange(projects.SelectMany(p => p.Documents));
        else
            documents.AddRange(_host.FindDocuments(scope!));

        if (documents.Count == 0)
            throw new RpcException("file_not_in_workspace",
                $"scope '{scope}' matched no project and no document");

        var exclusions = new List<string>
        {
            "generated files",
            "overrides and interface implementations",
            "[WebMethod], [Obsolete], [Serializable]",
            "Page_/Application_/Session_ handlers and (object, EventArgs) signatures",
            "Main and constructors",
            "public and protected members (reachable from outside this scope)",
        };

        var results = new List<object>();
        var considered = 0;

        foreach (var doc in documents)
        {
            var model = await doc.GetSemanticModelAsync().ConfigureAwait(false);
            var root = await doc.GetSyntaxRootAsync().ConfigureAwait(false);
            if (model is null || root is null) continue;

            foreach (var node in root.DescendantNodes())
            {
                if (node is not MemberDeclarationSyntax) continue;
                var sym = model.GetDeclaredSymbol(node);
                if (sym is null) continue;
                if (!IsDeadCodeCandidate(sym)) continue;
                considered++;
                var refs = await SymbolFinder.FindReferencesAsync(sym, sln)
                                             .ConfigureAwait(false);
                if (refs.SelectMany(r => r.Locations).Any()) continue;
                results.Add(SymbolDto(sym));
                if (results.Count >= max) break;
            }
            if (results.Count >= max) break;
        }

        return new
        {
            scope,
            candidates_considered = considered,
            unreferenced = results,
            exclusions_applied = exclusions,
            truncated = results.Count >= max,
        };
    }

    private static bool IsDeadCodeCandidate(ISymbol s)
    {
        if (s.IsImplicitlyDeclared) return false;
        if (s.DeclaredAccessibility is not (Accessibility.Private or Accessibility.Internal))
            return false;
        if (s.IsOverride) return false;
        if (s.Kind == SymbolKind.NamedType) return true;

        if (s is IMethodSymbol m)
        {
            if (m.MethodKind is not (MethodKind.Ordinary)) return false;
            if (m.Name == "Main") return false;
            if (m.Name.StartsWith("Page_") || m.Name.StartsWith("Application_")
                || m.Name.StartsWith("Session_")) return false;
            if (m.Parameters.Length == 2 &&
                m.Parameters[1].Type.Name.EndsWith("EventArgs")) return false;
            if (ImplementsInterface(m)) return false;
        }
        else if (s is not (IPropertySymbol or IFieldSymbol or IEventSymbol))
        {
            return false;
        }

        foreach (var a in s.GetAttributes())
        {
            var n = a.AttributeClass?.Name ?? "";
            if (n is "WebMethodAttribute" or "ObsoleteAttribute" or "SerializableAttribute")
                return false;
        }
        return true;
    }

    private static bool ImplementsInterface(IMethodSymbol m)
    {
        var type = m.ContainingType;
        if (type is null) return false;
        foreach (var iface in type.AllInterfaces)
            foreach (var member in iface.GetMembers().OfType<IMethodSymbol>())
                if (SymbolEqualityComparer.Default.Equals(
                        type.FindImplementationForInterfaceMember(member), m))
                    return true;
        return false;
    }

    public async Task<object> DiagnosticsSummary(string? projectName)
    {
        var sln = _host.Solution;
        var projects = string.IsNullOrWhiteSpace(projectName)
            ? sln.Projects.ToList()
            : sln.Projects.Where(p => string.Equals(p.Name, projectName,
                StringComparison.OrdinalIgnoreCase)).ToList();

        var errors = 0;
        var warnings = 0;
        var top = new Dictionary<string, int>();

        foreach (var p in projects)
        {
            var comp = await p.GetCompilationAsync().ConfigureAwait(false);
            if (comp is null) continue;
            foreach (var d in comp.GetDiagnostics())
            {
                if (d.Severity == DiagnosticSeverity.Error) errors++;
                else if (d.Severity == DiagnosticSeverity.Warning) warnings++;
                else continue;
                top[d.Id] = top.TryGetValue(d.Id, out var c) ? c + 1 : 1;
            }
        }

        return new
        {
            projects = projects.Select(p => p.Name).ToList(),
            errors,
            warnings,
            top = top.OrderByDescending(kv => kv.Value).Take(20)
                     .Select(kv => new { id = kv.Key, count = kv.Value }).ToList(),
        };
    }
}
