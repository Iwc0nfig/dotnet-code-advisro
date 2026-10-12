using System.Text.RegularExpressions;

namespace DotnetCodeAdvisor.RoslynHost;

/// <summary>
/// Minimal .sln reader.  Exists for one reason: an ASP.NET Website Project has
/// no project file, so MSBuildWorkspace skips it entirely.  Everything needed to
/// rebuild it as a Roslyn project lives in the .sln's WebsiteProperties section,
/// and is read from there rather than hard-coded.
/// </summary>
public static class SolutionReader
{
    // Project("{TypeGuid}") = "Name", "Path", "{ProjectGuid}"
    private static readonly Regex ProjectLine = new(
        @"^Project\(""\{(?<type>[^}]+)\}""\)\s*=\s*""(?<name>[^""]*)""\s*,\s*""(?<path>[^""]*)""\s*,\s*""\{(?<guid>[^}]+)\}""",
        RegexOptions.Compiled | RegexOptions.Multiline);

    private static readonly Regex ProjectRefs = new(
        @"ProjectReferences\s*=\s*""(?<refs>[^""]*)""",
        RegexOptions.Compiled);

    public const string WebsiteTypeGuid = "E24C65DC-7377-472B-9ABA-BC803B73C61A";
    public const string SolutionFolderGuid = "2150E333-8FDC-42A3-9474-1A3956D46DE8";

    public sealed record SlnProject(string TypeGuid, string Name, string Path, string Guid)
    {
        public bool IsSolutionFolder =>
            string.Equals(TypeGuid, SolutionFolderGuid, StringComparison.OrdinalIgnoreCase);
        public bool IsWebsite =>
            string.Equals(TypeGuid, WebsiteTypeGuid, StringComparison.OrdinalIgnoreCase);
        public bool IsProjectFile =>
            !IsSolutionFolder && !IsWebsite &&
            System.IO.Path.HasExtension(Path) &&
            Path.EndsWith("proj", StringComparison.OrdinalIgnoreCase);
    }

    public static List<SlnProject> ReadProjects(string slnPath)
    {
        var text = File.ReadAllText(slnPath);
        var list = new List<SlnProject>();
        foreach (Match m in ProjectLine.Matches(text))
        {
            list.Add(new SlnProject(
                m.Groups["type"].Value, m.Groups["name"].Value,
                m.Groups["path"].Value, m.Groups["guid"].Value));
        }
        return list;
    }

    /// <summary>Loadable project files only -- what MSBuildWorkspace will open.</summary>
    public static int CountProjects(string slnPath)
    {
        try
        {
            return ReadProjects(slnPath).Count(p => p.IsProjectFile);
        }
        catch
        {
            return 0;
        }
    }

    /// <summary>
    /// The names of the projects the Website Project references, resolved from
    /// the GUID list in its WebsiteProperties section against the .sln's own
    /// Project() lines.  Never hard-coded: if the solution changes, we follow it.
    /// </summary>
    public static List<string> ReadWebsiteReferences(string slnPath)
    {
        var text = File.ReadAllText(slnPath);
        var byGuid = ReadProjects(slnPath)
            .ToDictionary(p => p.Guid.ToUpperInvariant(), p => p.Name);

        var names = new List<string>();
        var m = ProjectRefs.Match(text);
        if (!m.Success) return names;

        foreach (var entry in m.Groups["refs"].Value.Split(';'))
        {
            if (string.IsNullOrWhiteSpace(entry)) continue;
            var bar = entry.IndexOf('|');
            var guidPart = (bar >= 0 ? entry.Substring(0, bar) : entry)
                .Trim().Trim('{', '}').ToUpperInvariant();
            if (byGuid.TryGetValue(guidPart, out var name) && !names.Contains(name))
                names.Add(name);
        }
        return names;
    }

    public static SlnProject? FindWebsite(string slnPath) =>
        ReadProjects(slnPath).FirstOrDefault(p => p.IsWebsite);
}
