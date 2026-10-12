using System.Diagnostics;

namespace DotnetCodeAdvisor.RoslynHost;

/// <summary>
/// -- the go/no-go gate.  Measures a cold MSBuildWorkspace load:
/// elapsed time, peak working set, projects loaded, and the WorkspaceFailed list.
/// The result decides the deployment shape, so it runs before anything depends
/// on it.
///
///   usage: RoslynHost --bench &lt;solution&gt; [--side net48|net8]
///                     [--website &lt;root&gt;] [--framework-refs &lt;dir&gt;]
///                     [--designer-dir &lt;dir&gt;] [--gate] [--report &lt;json&gt;]
///
/// --gate compiles every loaded project (Gate.RunAsync) and --report writes the
/// load failures and per-project gate results, untruncated, as JSON.
/// </summary>
public static class Bench
{
    public static async Task<int> RunAsync(WorkspaceHost host, string[] args)
    {
        string? sln = null, website = null, refs = null, designers = null, side = "unknown";
        string? report = null;
        var gate = false;
        var props = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        for (var i = 1; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--side": side = Next(args, ref i); break;
                case "--website": website = Next(args, ref i); break;
                case "--framework-refs": refs = Next(args, ref i); break;
                case "--designer-dir": designers = Next(args, ref i); break;
                case "--gate": gate = true; break;
                case "--report": report = Next(args, ref i); break;
                case "--prop":
                    var kv = Next(args, ref i) ?? "";
                    var eq = kv.IndexOf('=');
                    if (eq > 0) props[kv.Substring(0, eq)] = kv.Substring(eq + 1);
                    break;
                default:
                    if (sln is null) sln = args[i];
                    break;
            }
        }
        if (sln is null)
        {
            Console.Error.WriteLine("usage: RoslynHost --bench <solution.sln> [--side s] "
                                    + "[--website dir] [--framework-refs dir] [--designer-dir dir]");
            return 2;
        }

        WebsiteSpec? spec = null;
        if (website is not null)
        {
            spec = new WebsiteSpec
            {
                WebsiteRoot = website,
                ReferencedProjectNames = SolutionReader.ReadWebsiteReferences(sln),
                SourceFiles = WebsiteProjectBuilder.CollectSourceFiles(website),
                BinDlls = WebsiteProjectBuilder.CollectBinDlls(website),
            };
            if (refs is not null)
                spec.FrameworkRefAssemblies = WebsiteProjectBuilder.CollectFrameworkRefs(refs);
            if (designers is not null && Directory.Exists(designers))
                foreach (var f in Directory.EnumerateFiles(designers, "*.synth.designer.cs"))
                    spec.SynthesizedDesigners.Add(new(f, File.ReadAllText(f)));

            Console.Error.WriteLine(
                $"[bench] website: {spec.SourceFiles.Count} source files, "
                + $"{spec.SynthesizedDesigners.Count} synthesised designers, "
                + $"{spec.BinDlls.Count} Bin dlls, "
                + $"{spec.FrameworkRefAssemblies.Count} framework refs, "
                + $"refs -> [{string.Join(", ", spec.ReferencedProjectNames)}]");
        }

        var sw = Stopwatch.StartNew();
        var ok = true;
        string? failure = null;
        try
        {
            await host.LoadAsync(sln, spec, side ?? "unknown",
                (l, t, name) =>
                {
                    if (l % 5 == 0 || l == t)
                        Console.Error.WriteLine($"[bench] {l}/{t} {name}");
                },
                CancellationToken.None, props).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            ok = false;
            failure = ex.ToString();
        }
        sw.Stop();

        var proc = Process.GetCurrentProcess();
        proc.Refresh();
        var peakMb = proc.PeakWorkingSet64 / (1024.0 * 1024.0);

        Console.WriteLine("========================================================");
        Console.WriteLine($"solution        : {sln}");
        Console.WriteLine($"side            : {side}");
        Console.WriteLine($"loaded          : {ok}");
        Console.WriteLine($"elapsed         : {sw.Elapsed.TotalSeconds:F1} s");
        Console.WriteLine($"peak working set: {peakMb:F0} MB");
        Console.WriteLine($"projects        : {host.Loaded}/{host.Total}");
        Console.WriteLine($"website docs    : {host.WebsiteDocuments}");
        Console.WriteLine($"workspace failures: {host.Failures.Count} (+{host.Warnings.Count} warnings)");
        Console.WriteLine($"razor projects  : {host.RazorProjects}");
        foreach (var f in host.Failures.Take(25))
            Console.WriteLine("   ! " + Trim(f));
        if (host.Failures.Count > 25)
            Console.WriteLine($"   ... and {host.Failures.Count - 25} more");

        if (ok)
        {
            Console.WriteLine("--- projects loaded ---");
            foreach (var p in host.Solution.Projects.OrderBy(p => p.Name))
                Console.WriteLine($"   {p.Name,-42} {p.DocumentIds.Count,6} docs");
        }
        else
        {
            Console.WriteLine("--- load failed ---");
            Console.WriteLine(failure);
        }

        List<Gate.ProjectGate>? gates = null;
        if (ok && gate)
        {
            var gsw = Stopwatch.StartNew();
            gates = await Gate.RunAsync(host.Solution, m => Console.Error.WriteLine(m),
                                        CancellationToken.None).ConfigureAwait(false);
            proc.Refresh();
            Console.WriteLine("--- gate ---");
            Console.WriteLine($"compile elapsed : {gsw.Elapsed.TotalSeconds:F1} s");
            Console.WriteLine($"peak working set: {proc.PeakWorkingSet64 / (1024.0 * 1024.0):F0} MB");
            Console.WriteLine($"clean projects  : {gates.Count(g => g.Clean)}/{gates.Count}");
            Console.WriteLine($"binding errors  : {gates.Sum(g => g.BindingErrors)}");
            Console.WriteLine($"generated docs  : {gates.Sum(g => g.GeneratedDocuments)}");
        }

        if (report is not null)
        {
            var json = System.Text.Json.JsonSerializer.Serialize(new
            {
                solution = sln,
                side,
                loaded = ok,
                elapsed_s = sw.Elapsed.TotalSeconds,
                projects_total = host.Total,
                failures = host.Failures,
                warnings = host.Warnings,
                gate = gates,
            }, new System.Text.Json.JsonSerializerOptions { WriteIndented = true });
            File.WriteAllText(report, json);
            Console.WriteLine($"report          : {report}");
        }

        var verdict = sw.Elapsed.TotalMinutes switch
        {
            < 5 => "GOOD: run both hosts as long-lived processes with the MCP server.",
            < 15 => "WORKABLE: daemonise the hosts separately so load is paid once per boot.",
            _ => "CONTINGENCY: split the solution into project groups.",
        };
        Console.WriteLine($"verdict         : {verdict}");
        Console.WriteLine("========================================================");
        return ok ? 0 : 1;
    }

    private static string? Next(string[] args, ref int i) =>
        i + 1 < args.Length ? args[++i] : null;

    private static string Trim(string s)
    {
        s = s.Replace("\r", " ").Replace("\n", " ");
        return s.Length > 180 ? s.Substring(0, 180) + "..." : s;
    }
}
