using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.MSBuild;

namespace DotnetCodeAdvisor.RoslynHost;

/// <summary>
/// Owns the MSBuildWorkspace and the grafted Solution.  Loaded once, kept warm,
/// never reloaded in place: a half-reloaded workspace produces confidently wrong
/// references, which is the one failure mode this whole design exists to avoid.
/// </summary>
public sealed class WorkspaceHost
{
    private MSBuildWorkspace? _ws;
    private Solution? _solution;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly List<string> _failures = new();
    private readonly List<string> _warnings = new();

    public string State { get; private set; } = "cold";
    public int Loaded { get; private set; }
    public int Total { get; private set; }
    public string Side { get; private set; } = "unknown";
    public string? SolutionPath { get; private set; }
    public long LoadElapsedMs { get; private set; }
    public int WebsiteDocuments { get; private set; }
    public IReadOnlyList<string> Failures => _failures;

    /// <summary>
    /// WorkspaceFailed events of kind Warning (e.g. MSB3270 processor-architecture
    /// mismatches).  Kept apart from Failures: they do not drop anything from the
    /// compilation, and counting them as failures hid the ones that do.
    /// </summary>
    public IReadOnlyList<string> Warnings => _warnings;

    /// <summary>Projects whose Razor source generator was un-suppressed.</summary>
    public int RazorProjects { get; private set; }

    public Solution Solution =>
        _solution ?? throw new RpcException("not_loaded",
            $"Solution not loaded (state={State}, {Loaded}/{Total} projects)");

    public bool IsReady => _solution is not null && State == "ready";

    public async Task LoadAsync(string solutionPath, WebsiteSpec? website, string side,
                                Action<int, int, string> onProgress, CancellationToken ct,
                                IReadOnlyDictionary<string, string>? extraProperties = null)
    {
        await _gate.WaitAsync(ct).ConfigureAwait(false);
        var sw = System.Diagnostics.Stopwatch.StartNew();
        try
        {
            State = "loading";
            Side = side;
            SolutionPath = solutionPath;
            _failures.Clear();
            _warnings.Clear();
            Loaded = 0;
            RazorProjects = 0;

            var props = new Dictionary<string, string>
            {
                ["Configuration"] = "Debug",
                ["Platform"] = "AnyCPU",
                // Never let workspace loading trigger a real build or restore.
                ["DesignTimeBuild"] = "true",
                ["BuildingInsideVisualStudio"] = "true",
                ["SkipCompilerExecution"] = "true",
                ["ProvideCommandLineArgs"] = "true",
            };
            if (extraProperties is not null)
                foreach (var kv in extraProperties) props[kv.Key] = kv.Value;

            _ws = MSBuildWorkspace.Create(props);
            _ws.LoadMetadataForReferencedProjects = true;
            // Stops the Website Project entry and any Delphi/VB folders from
            // aborting the whole load.
            _ws.SkipUnrecognizedProjects = true;
            _ws.WorkspaceFailed += (_, e) =>
            {
                // MSB3270 (processor-architecture mismatch) arrives with Failure
                // severity but is an MSBuild warning that drops nothing.
                var warning = e.Diagnostic.Kind == WorkspaceDiagnosticKind.Warning
                              || e.Diagnostic.Message.IndexOf("mismatch between the processor architecture",
                                                              StringComparison.OrdinalIgnoreCase) >= 0;
                var list = warning ? _warnings : _failures;
                lock (list)
                {
                    if (list.Count < 2000) list.Add(e.Diagnostic.Message);
                }
                Console.Error.WriteLine("[host] " + e.Diagnostic);
            };

            Total = SolutionReader.CountProjects(solutionPath);

            var progress = new Progress<ProjectLoadProgress>(p =>
            {
                if (p.Operation == ProjectLoadOperation.Resolve)
                {
                    Loaded++;
                    onProgress(Loaded, Total,
                        Path.GetFileNameWithoutExtension(p.FilePath) ?? "");
                }
            });

            var sln = await _ws.OpenSolutionAsync(solutionPath, progress, ct)
                               .ConfigureAwait(false);

            sln = EnableRazorGenerator(sln);

            if (website is not null)
            {
                var before = sln.ProjectIds.Count;
                sln = WebsiteProjectBuilder.Graft(sln, website);
                WebsiteDocuments = sln.Projects
                    .FirstOrDefault(p => p.Name == WebsiteProjectBuilder.ProjectName)
                    ?.DocumentIds.Count ?? 0;
                Console.Error.WriteLine(
                    $"[host] grafted {WebsiteProjectBuilder.ProjectName}: " +
                    $"{WebsiteDocuments} documents, projects {before} -> {sln.ProjectIds.Count}");
            }

            _solution = sln;
            State = "ready";
        }
        catch (Exception ex)
        {
            State = "failed";
            _failures.Add(ex.ToString());
            throw;
        }
        finally
        {
            LoadElapsedMs = sw.ElapsedMilliseconds;
            _gate.Release();
        }
    }

    /// <summary>
    /// The Razor SDK adds a global analyzer config setting
    /// build_property.SuppressRazorSourceGenerator = true whenever DesignTimeBuild
    /// and BuildingInsideVisualStudio are both true, because Visual Studio
    /// compiles views out of process (Microsoft.NET.Sdk.Razor.SourceGenerators.targets,
    /// _PrepareRazorSourceGenerators).  We set both to stop MSBuild building, so
    /// without this every .cshtml/.razor file would be absent from the compilation
    /// and every reference from a view would be missing.
    /// </summary>
    private Solution EnableRazorGenerator(Solution sln)
    {
        foreach (var pid in sln.ProjectIds.ToList())
        {
            var proj = sln.GetProject(pid)!;
            var suppress = proj.AnalyzerConfigDocuments
                .Where(d => d.FilePath is not null
                            && d.FilePath.EndsWith("RazorSourceGenerator.razorencconfig",
                                                   StringComparison.OrdinalIgnoreCase))
                .Select(d => d.Id)
                .ToList();
            if (suppress.Count == 0) continue;
            sln = sln.RemoveAnalyzerConfigDocuments(System.Collections.Immutable.ImmutableArray.CreateRange(suppress));
            RazorProjects++;
        }
        if (RazorProjects > 0)
            Console.Error.WriteLine($"[host] Razor source generator enabled in {RazorProjects} project(s)");
        return sln;
    }

    /// <summary>Documents whose FilePath matches, across every project.</summary>
    public IEnumerable<Document> FindDocuments(string filePath)
    {
        var norm = Norm(filePath);
        foreach (var proj in Solution.Projects)
        {
            foreach (var doc in proj.Documents)
            {
                if (doc.FilePath is not null && Norm(doc.FilePath) == norm)
                    yield return doc;
            }
        }
    }

    private static string Norm(string p) =>
        p.Replace('\\', '/').TrimEnd('/').ToLowerInvariant();
}
