using System.Text.Json;
using System.Text.Json.Serialization;

namespace DotnetCodeAdvisor.RoslynHost;

public sealed class RpcException : Exception
{
    public string Code { get; }
    public RpcException(string code, string message) : base(message) => Code = code;
}

public sealed class Request
{
    [JsonPropertyName("id")] public int? Id { get; set; }
    [JsonPropertyName("method")] public string Method { get; set; } = "";
    [JsonPropertyName("params")] public JsonElement Params { get; set; }
}

/// <summary>
/// Newline-delimited JSON over stdio: one object per line, no indentation.
/// stdout carries protocol only; all logging goes to stderr.  Position
/// convention is 1-based line and 1-based column in both directions -- Roslyn's
/// LinePosition is 0-based and is converted here, at the boundary, and nowhere
/// else.
/// </summary>
public sealed class Rpc
{
    private readonly WorkspaceHost _host;
    private readonly Handlers _handlers;

    private static readonly JsonSerializerOptions Json = new()
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        WriteIndented = false,
    };

    public Rpc(WorkspaceHost host)
    {
        _host = host;
        _handlers = new Handlers(host);
    }

    public async Task<string> DispatchAsync(string line)
    {
        Request? req;
        try
        {
            req = JsonSerializer.Deserialize<Request>(line);
        }
        catch (JsonException ex)
        {
            return Error(null, "invalid_params", "malformed JSON: " + ex.Message);
        }
        if (req is null || string.IsNullOrEmpty(req.Method))
            return Error(null, "invalid_params", "missing 'method'");

        try
        {
            var result = await Route(req).ConfigureAwait(false);
            return JsonSerializer.Serialize(new { id = req.Id, ok = true, result }, Json);
        }
        catch (RpcException ex)
        {
            return Error(req.Id, ex.Code, ex.Message);
        }
        catch (OperationCanceledException)
        {
            return Error(req.Id, "timeout", "query cancelled or timed out");
        }
        catch (Exception ex)
        {
            return Error(req.Id, "internal", ex.ToString());
        }
    }

    private async Task<object?> Route(Request req)
    {
        var p = req.Params;
        switch (req.Method)
        {
            case "ping":
                return new
                {
                    side = _host.Side,
                    state = _host.State,
                    projects_loaded = _host.Loaded,
                    projects_total = _host.Total,
                    elapsed_ms = _host.LoadElapsedMs,
                    website_documents = _host.WebsiteDocuments,
                    failures = _host.Failures.Take(50).ToArray(),
                    failure_count = _host.Failures.Count,
                    solution = _host.SolutionPath,
                };

            case "load":
                {
                    var sln = Str(p, "solution") ?? throw new RpcException(
                        "invalid_params", "load requires 'solution'");
                    var side = Str(p, "side") ?? "unknown";
                    WebsiteSpec? spec = null;
                    if (p.TryGetProperty("website", out var w) &&
                        w.ValueKind == JsonValueKind.Object)
                    {
                        spec = BuildWebsiteSpec(w, sln);
                    }
                    await _host.LoadAsync(sln, spec, side, (l, t, name) =>
                    {
                        if (l % 5 == 0 || l == t)
                        {
                            Console.Out.Write(JsonSerializer.Serialize(new
                            {
                                progress = new { loaded = l, total = t, current = name }
                            }, Json));
                            Console.Out.Write('\n');
                            Console.Out.Flush();
                        }
                    }, CancellationToken.None).ConfigureAwait(false);
                    return new
                    {
                        state = _host.State,
                        projects_loaded = _host.Loaded,
                        projects_total = _host.Total,
                        elapsed_ms = _host.LoadElapsedMs,
                        website_documents = _host.WebsiteDocuments,
                        failure_count = _host.Failures.Count,
                    };
                }

            case "symbol_at":
                return await _handlers.SymbolAt(Str(p, "file")!, Int(p, "line"), Int(p, "col"))
                                      .ConfigureAwait(false);
            case "find_symbol":
                return await _handlers.FindSymbol(Str(p, "query")!, Int(p, "max", 50))
                                      .ConfigureAwait(false);
            case "go_to_definition":
                return await _handlers.GoToDefinition(Str(p, "file")!, Int(p, "line"), Int(p, "col"))
                                      .ConfigureAwait(false);
            case "find_references":
                return await _handlers.FindReferences(
                    Str(p, "file")!, Int(p, "line"), Int(p, "col"), Int(p, "max", 100))
                    .ConfigureAwait(false);
            case "find_callers":
                return await _handlers.FindCallers(
                    Str(p, "file")!, Int(p, "line"), Int(p, "col"),
                    Int(p, "depth", 2), Int(p, "max", 100)).ConfigureAwait(false);
            case "find_implementations":
                return await _handlers.FindImplementations(
                    Str(p, "file")!, Int(p, "line"), Int(p, "col"), Int(p, "max", 100))
                    .ConfigureAwait(false);
            case "type_info":
                return await _handlers.TypeInfo(Str(p, "file")!, Int(p, "line"), Int(p, "col"))
                                      .ConfigureAwait(false);
            case "project_graph":
                return _handlers.ProjectGraph();
            case "dead_code":
                return await _handlers.DeadCode(Str(p, "scope"), Int(p, "max", 100))
                                      .ConfigureAwait(false);
            case "diagnostics_summary":
                return await _handlers.DiagnosticsSummary(Str(p, "project"))
                                      .ConfigureAwait(false);
            case "shutdown":
                Environment.Exit(0);
                return null;
            default:
                throw new RpcException("invalid_params", $"unknown method '{req.Method}'");
        }
    }

    private static WebsiteSpec BuildWebsiteSpec(JsonElement w, string slnPath)
    {
        var root = Str(w, "root") ?? throw new RpcException(
            "invalid_params", "website.root is required");
        var spec = new WebsiteSpec
        {
            WebsiteRoot = root,
            ReferencedProjectNames = SolutionReader.ReadWebsiteReferences(slnPath),
            SourceFiles = WebsiteProjectBuilder.CollectSourceFiles(root),
            BinDlls = WebsiteProjectBuilder.CollectBinDlls(root),
        };
        var refDir = Str(w, "framework_refs");
        if (!string.IsNullOrEmpty(refDir))
            spec.FrameworkRefAssemblies = WebsiteProjectBuilder.CollectFrameworkRefs(refDir!);

        if (w.TryGetProperty("designers", out var d) && d.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in d.EnumerateArray())
            {
                var path = Str(item, "path");
                var text = Str(item, "text");
                if (path is not null && text is not null)
                    spec.SynthesizedDesigners.Add(new(path, text));
            }
        }
        else if (w.TryGetProperty("designer_dir", out var dd) && dd.ValueKind == JsonValueKind.String)
        {
            var dir = dd.GetString()!;
            if (Directory.Exists(dir))
            {
                foreach (var f in Directory.EnumerateFiles(dir, "*.synth.designer.cs"))
                    spec.SynthesizedDesigners.Add(new(f, File.ReadAllText(f)));
            }
        }
        return spec;
    }

    private static string Error(int? id, string code, string message) =>
        JsonSerializer.Serialize(new
        {
            id,
            ok = false,
            error = new { code, message }
        }, Json);

    private static string? Str(JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object &&
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() : null;

    private static int Int(JsonElement e, string name, int fallback = 0) =>
        e.ValueKind == JsonValueKind.Object &&
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number
            ? v.GetInt32() : fallback;
}
