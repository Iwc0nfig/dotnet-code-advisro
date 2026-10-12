using Microsoft.CodeAnalysis;

namespace DotnetCodeAdvisor.RoslynHost;

/// <summary>
/// The completeness gate.  Compiles every project in the Solution and counts the
/// errors that mean "the compiler could not see something" -- a missing type,
/// namespace, member or assembly, or the same type arriving from two places.
/// A project with any of those produces references that are silently missing, so
/// the count is recorded per project rather than summed away.
///
/// Compiling in memory binds exactly what csc binds; what this measures is
/// whether the INPUTS (references, generated code, framework assemblies) were
/// complete.
/// </summary>
public static class Gate
{
    /// <summary>
    /// Errors that mean a name did not bind.  Every one of them leaves a hole in
    /// find_references: the use site exists in source but points at nothing.
    /// </summary>
    public static readonly HashSet<string> BindingErrorIds = new(StringComparer.Ordinal)
    {
        "CS0012", // type defined in an assembly that is not referenced
        "CS0103", // name does not exist in the current context
        "CS0117", // type does not contain a definition for member
        "CS0234", // type or namespace does not exist in namespace
        "CS0246", // type or namespace could not be found
        "CS0400", // type or namespace could not be found in the global namespace
        "CS0426", // type name does not exist in type
        "CS0433", // type exists in both A and B
        "CS0518", // predefined type is not defined or imported (no mscorlib)
        "CS1061", // type does not contain a definition and no extension method
        "CS1704", // assembly with the same simple name already imported
    };

    public sealed class ProjectGate
    {
        public string Name { get; set; } = "";
        public string? FilePath { get; set; }
        public string? AssemblyName { get; set; }
        public int Documents { get; set; }
        public int GeneratedDocuments { get; set; }
        public int MetadataReferences { get; set; }
        public int ProjectReferences { get; set; }
        public int Errors { get; set; }
        public int BindingErrors { get; set; }
        public int Warnings { get; set; }
        public double ElapsedSeconds { get; set; }
        public string? Failure { get; set; }
        public List<CodeCount> TopErrors { get; set; } = new();
        public List<ErrorItem> SampleErrors { get; set; } = new();
        public bool Clean => Failure is null && BindingErrors == 0;
    }

    public sealed class CodeCount
    {
        public string Id { get; set; } = "";
        public int Count { get; set; }
    }

    public sealed class ErrorItem
    {
        public string Id { get; set; } = "";
        public string? File { get; set; }
        public int Line { get; set; }
        public string Message { get; set; } = "";
    }

    public static async Task<List<ProjectGate>> RunAsync(
        Solution sln, Action<string> log, CancellationToken ct, int sampleErrors = 10)
    {
        var outp = new List<ProjectGate>();
        var projects = sln.Projects.OrderBy(p => p.Name, StringComparer.Ordinal).ToList();
        var i = 0;
        foreach (var p in projects)
        {
            ct.ThrowIfCancellationRequested();
            i++;
            var g = new ProjectGate
            {
                Name = p.Name,
                FilePath = p.FilePath,
                AssemblyName = p.AssemblyName,
                Documents = p.DocumentIds.Count,
                MetadataReferences = p.MetadataReferences.Count,
                ProjectReferences = p.ProjectReferences.Count(),
            };
            var sw = System.Diagnostics.Stopwatch.StartNew();
            try
            {
                g.GeneratedDocuments = (await p.GetSourceGeneratedDocumentsAsync(ct)
                                               .ConfigureAwait(false)).Count();
                var comp = await p.GetCompilationAsync(ct).ConfigureAwait(false);
                if (comp is null)
                {
                    g.Failure = "no compilation (language without compiler support?)";
                }
                else
                {
                    var top = new Dictionary<string, int>(StringComparer.Ordinal);
                    foreach (var d in comp.GetDiagnostics(ct))
                    {
                        if (d.Severity == DiagnosticSeverity.Warning) { g.Warnings++; continue; }
                        if (d.Severity != DiagnosticSeverity.Error) continue;
                        g.Errors++;
                        var binding = BindingErrorIds.Contains(d.Id);
                        if (binding) g.BindingErrors++;
                        top[d.Id] = top.TryGetValue(d.Id, out var c) ? c + 1 : 1;
                        if (g.SampleErrors.Count < sampleErrors && (binding || g.SampleErrors.Count < sampleErrors / 2))
                        {
                            var span = d.Location.GetMappedLineSpan();
                            g.SampleErrors.Add(new ErrorItem
                            {
                                Id = d.Id,
                                File = span.Path,
                                Line = span.StartLinePosition.Line + 1,
                                Message = d.GetMessage(),
                            });
                        }
                    }
                    // GetDiagnostics returns them in the order concurrent binding
                    // finished; sort so the recorded list is the same every run.
                    g.SampleErrors = g.SampleErrors
                        .OrderBy(e => e.File ?? "", StringComparer.Ordinal)
                        .ThenBy(e => e.Line)
                        .ThenBy(e => e.Id, StringComparer.Ordinal)
                        .ThenBy(e => e.Message, StringComparer.Ordinal)
                        .ToList();
                    g.TopErrors = top.OrderByDescending(kv => kv.Value)
                                     .ThenBy(kv => kv.Key, StringComparer.Ordinal)
                                     .Take(10)
                                     .Select(kv => new CodeCount { Id = kv.Key, Count = kv.Value })
                                     .ToList();
                }
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                g.Failure = ex.GetType().Name + ": " + ex.Message;
            }
            g.ElapsedSeconds = sw.Elapsed.TotalSeconds;
            outp.Add(g);
            log($"[gate] {i}/{projects.Count} {p.Name}: {g.Errors} errors "
                + $"({g.BindingErrors} binding), {g.GeneratedDocuments} generated docs, "
                + $"{g.ElapsedSeconds:F1}s");
        }
        return outp;
    }
}
