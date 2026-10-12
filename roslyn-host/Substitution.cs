using Microsoft.CodeAnalysis;

namespace DotnetCodeAdvisor.RoslynHost;

/// <summary>
/// Replaces binary references to our OWN code with project references.
///
/// Many projects reference another project's build output instead of the
/// project (HintPath ..\App\Bin\Modern.dll, ..\X\bin\Debug\X.dll), and the
/// App/ site references Bin copies of the very projects it also references as
/// source.  A use of Modern.Foo through a DLL binds to a metadata symbol,
/// so find_references on the source declaration never sees it -- or, with both
/// the DLL and the project present, binds to neither (CS0433).  Substituting
/// the source project makes every such use a source reference.
///
/// Only unambiguous cases are substituted.  Several projects building the same
/// assembly name (several produce the same assembly) or a substitution that would
/// create a project cycle is recorded and left as it was: a guess here would be
/// a silently wrong edge.
/// </summary>
public static class Substitution
{
    public sealed class Record
    {
        public string Project { get; set; } = "";
        public string Reference { get; set; } = "";
        public string Action { get; set; } = "";   // substituted | duplicate_removed | ambiguous | cycle
        public string? Target { get; set; }
        public int Candidates { get; set; }
    }

    public static Solution Apply(Solution sln, List<Record> log)
    {
        var producers = sln.Projects
            .Where(p => !string.IsNullOrEmpty(p.AssemblyName))
            .GroupBy(p => p.AssemblyName, StringComparer.OrdinalIgnoreCase)
            .ToDictionary(g => g.Key, g => g.Select(p => p.Id).ToList(), StringComparer.OrdinalIgnoreCase);

        foreach (var pid in sln.Projects.OrderBy(p => p.FilePath ?? p.Name, StringComparer.OrdinalIgnoreCase)
                                        .Select(p => p.Id).ToList())
        {
            var proj = sln.GetProject(pid)!;
            foreach (var mref in proj.MetadataReferences.OfType<PortableExecutableReference>().ToList())
            {
                if (mref.FilePath is null) continue;
                var name = Path.GetFileNameWithoutExtension(mref.FilePath);
                if (!producers.TryGetValue(name, out var ids)) continue;
                var others = ids.Where(i => i != pid).ToList();
                if (others.Count == 0) continue;

                var rec = new Record
                {
                    Project = Describe(proj),
                    Reference = mref.FilePath,
                    Candidates = others.Count,
                };
                if (others.Count > 1)
                {
                    rec.Action = "ambiguous";
                    log.Add(rec);
                    continue;
                }

                var target = others[0];
                rec.Target = Describe(sln.GetProject(target)!);
                proj = sln.GetProject(pid)!;
                if (proj.ProjectReferences.Any(r => r.ProjectId == target))
                {
                    sln = sln.RemoveMetadataReference(pid, mref);
                    rec.Action = "duplicate_removed";
                    log.Add(rec);
                    continue;
                }
                var graph = sln.GetProjectDependencyGraph();
                if (graph.GetProjectsThatThisProjectTransitivelyDependsOn(target).Contains(pid))
                {
                    rec.Action = "cycle";
                    log.Add(rec);
                    continue;
                }
                try
                {
                    sln = DropDangling(sln, pid)
                        .RemoveMetadataReference(pid, mref)
                        .AddProjectReference(pid, new ProjectReference(target, mref.Properties.Aliases));
                    rec.Action = "substituted";
                }
                catch (Exception ex)
                {
                    rec.Action = "failed: " + ex.GetType().Name + ": " + ex.Message;
                }
                log.Add(rec);
            }
        }
        return sln;
    }

    private static string Describe(Project p) => p.FilePath ?? p.Name;

    /// <summary>
    /// Remove project references to projects that are not in the Solution --
    /// what MSBuildWorkspace leaves behind for a ProjectReference whose .csproj
    /// failed to load.  They point at nothing, and Roslyn 4.8's
    /// AddProjectReference asserts on them (Contract.Fail, "Unexpected null").
    /// </summary>
    public static Solution DropDangling(Solution sln, ProjectId pid)
    {
        foreach (var r in sln.GetProject(pid)!.AllProjectReferences.ToList())
            if (sln.GetProject(r.ProjectId) is null)
                sln = sln.RemoveProjectReference(pid, r);
        return sln;
    }
}
