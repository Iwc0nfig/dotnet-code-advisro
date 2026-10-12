using System.Text;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.Text;

namespace DotnetCodeAdvisor.RoslynHost;

/// <summary>Everything needed to rebuild the primary Website Project in Roslyn.</summary>
public sealed class WebsiteSpec
{
    public string WebsiteRoot { get; set; } = "";
    public List<string> ReferencedProjectNames { get; set; } = new();
    public List<string> SourceFiles { get; set; } = new();
    public List<string> BinDlls { get; set; } = new();
    public List<string> FrameworkRefAssemblies { get; set; } = new();
    /// <summary>path -> text, for the partials ASP.NET generates at runtime.</summary>
    public List<KeyValuePair<string, string>> SynthesizedDesigners { get; set; } = new();
}

/// <summary>A Website Project as the build step describes it (index mode).</summary>
public sealed class ExactWebsite
{
    public string Name { get; set; } = "";
    public string Rel { get; set; } = "";
    public string Root { get; set; } = "";
    public List<string> ProjectPaths { get; set; } = new();
    public List<string> SourceFiles { get; set; } = new();
    public List<string> GeneratedFiles { get; set; } = new();
    public List<string> FallbackDesigners { get; set; } = new();
    public List<string> References { get; set; } = new();
    public string LangVersion { get; set; } = "7.3";
    public List<string> PreprocessorSymbols { get; set; } = new();
    public string ProjectName => Name + ".Website";
}

/// <summary>
/// Grafts the Website Project into the loaded Solution.  This is the piece that
/// makes sites without a .csproj semantically visible in the loaded workspace.
/// </summary>
public static class WebsiteProjectBuilder
{
    public const string ProjectName = "PrimaryWebsite.Website";

    public static Solution Graft(Solution sln, WebsiteSpec spec)
    {
        var projectId = ProjectId.CreateNewId(ProjectName);

        var refs = spec.ReferencedProjectNames
            .Select(n => sln.Projects.FirstOrDefault(p => p.Name == n))
            .Where(p => p is not null)
            .Select(p => new ProjectReference(p!.Id))
            .ToList();

        var metadata = spec.BinDlls
            .Concat(spec.FrameworkRefAssemblies)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .Where(File.Exists)
            .Select(p => (MetadataReference)MetadataReference.CreateFromFile(p))
            .ToList();

        var docs = new List<DocumentInfo>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        foreach (var f in spec.SourceFiles)
        {
            if (!seen.Add(f) || !File.Exists(f)) continue;
            docs.Add(DocumentInfo.Create(
                DocumentId.CreateNewId(projectId),
                Path.GetFileName(f),
                loader: new FileTextLoader(f, Encoding.UTF8),
                filePath: f));
        }

        foreach (var kv in spec.SynthesizedDesigners)
        {
            if (!seen.Add(kv.Key)) continue;
            docs.Add(DocumentInfo.Create(
                DocumentId.CreateNewId(projectId),
                Path.GetFileName(kv.Key),
                loader: TextLoader.From(TextAndVersion.Create(
                    SourceText.From(kv.Value, Encoding.UTF8), VersionStamp.Create(), kv.Key)),
                filePath: kv.Key));
        }

        var info = ProjectInfo.Create(
            projectId, VersionStamp.Create(), ProjectName, ProjectName,
            LanguageNames.CSharp,
            filePath: spec.WebsiteRoot,
            compilationOptions: new CSharpCompilationOptions(
                OutputKind.DynamicallyLinkedLibrary,
                // App_Code and pages routinely carry unused usings and assembly
                // version skew; we want symbols, not a clean build.
                specificDiagnosticOptions: new Dictionary<string, ReportDiagnostic>
                {
                    ["CS1701"] = ReportDiagnostic.Suppress,
                    ["CS1702"] = ReportDiagnostic.Suppress,
                }),
            // Explicit fallback for live synthesis. Persisted indexing instead
            // uses ASP.NET-generated source and its recorded language version.
            parseOptions: new CSharpParseOptions(LanguageVersion.CSharp7_3),
            documents: docs,
            projectReferences: refs,
            metadataReferences: metadata);

        // AddProject returns a NEW Solution, which we keep as our root.  We never
        // call TryApplyChanges -- MSBuildWorkspace refuses AddProject, and we do
        // not need it: every SymbolFinder API takes a Solution, so holding our own
        // grafted Solution is what gives cross-project find_references between
        // App/ code-behind and Modern / Demo.UI.
        return sln.AddProject(info);
    }

    /// <summary>Collect the C# files a Website Project compiles.</summary>
    public static List<string> CollectSourceFiles(string websiteRoot)
    {
        var outp = new List<string>();
        if (!Directory.Exists(websiteRoot)) return outp;
        foreach (var f in Directory.EnumerateFiles(websiteRoot, "*.cs", SearchOption.AllDirectories))
        {
            var rel = f.Substring(websiteRoot.Length).Replace('\\', '/');
            if (rel.IndexOf("/obj/", StringComparison.OrdinalIgnoreCase) >= 0) continue;
            if (rel.IndexOf("/bin/", StringComparison.OrdinalIgnoreCase) >= 0) continue;
            if (rel.IndexOf("/PrecompiledWeb/", StringComparison.OrdinalIgnoreCase) >= 0) continue;
            outp.Add(f);
        }
        return outp;
    }

    /// <summary>
    /// The index-mode graft: every input is an explicit list produced by the
    /// build step, nothing is globbed or guessed here.
    ///   * references are the exact assemblies ASP.NET compiles the site against
    ///     (MarkupCodegen's codegen.json), not "every DLL in Bin + every
    ///     framework assembly";
    ///   * markup is ASP.NET's own generated code, with synthesised designer
    ///     partials only for pages it could not generate;
    ///   * referenced projects are matched by .csproj path, not by name (names
    ///     repeat in this workspace).
    /// </summary>
    public static Solution GraftExact(Solution sln, ExactWebsite w, out ProjectId id)
    {
        id = ProjectId.CreateNewId(w.ProjectName);
        var byPath = sln.Projects
            .Where(p => p.FilePath is not null)
            .GroupBy(p => Path.GetFullPath(p.FilePath!), StringComparer.OrdinalIgnoreCase)
            .ToDictionary(g => g.Key, g => g.First().Id, StringComparer.OrdinalIgnoreCase);
        var refs = w.ProjectPaths
            .Select(p => byPath.TryGetValue(Path.GetFullPath(p), out var pid) ? pid : null)
            .Where(pid => pid is not null)
            .Distinct()
            .Select(pid => new ProjectReference(pid!))
            .ToList();

        var metadata = w.References
            .Where(File.Exists)
            .GroupBy(p => Path.GetFileName(p), StringComparer.OrdinalIgnoreCase)
            .Select(g => (MetadataReference)MetadataReference.CreateFromFile(g.First()))
            .ToList();

        var docs = new List<DocumentInfo>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var f in w.SourceFiles.Concat(w.GeneratedFiles).Concat(w.FallbackDesigners))
        {
            if (!seen.Add(f) || !File.Exists(f)) continue;
            docs.Add(DocumentInfo.Create(
                DocumentId.CreateNewId(id),
                Path.GetFileName(f),
                loader: new FileTextLoader(f, Encoding.UTF8),
                filePath: f));
        }

        var lang = LanguageVersionFacts.TryParse(w.LangVersion, out var lv) ? lv : LanguageVersion.CSharp7_3;
        var info = ProjectInfo.Create(
            id, VersionStamp.Create(), w.ProjectName, w.ProjectName,
            LanguageNames.CSharp,
            filePath: w.Root,
            compilationOptions: new CSharpCompilationOptions(
                OutputKind.DynamicallyLinkedLibrary,
                specificDiagnosticOptions: new Dictionary<string, ReportDiagnostic>
                {
                    ["CS1701"] = ReportDiagnostic.Suppress,
                    ["CS1702"] = ReportDiagnostic.Suppress,
                }),
            parseOptions: new CSharpParseOptions(lang, preprocessorSymbols: w.PreprocessorSymbols),
            documents: docs,
            projectReferences: refs,
            metadataReferences: metadata);
        return sln.AddProject(info);
    }

    public static List<string> CollectBinDlls(string websiteRoot)
    {
        var bin = Path.Combine(websiteRoot, "Bin");
        if (!Directory.Exists(bin)) return new List<string>();
        return Directory.EnumerateFiles(bin, "*.dll", SearchOption.TopDirectoryOnly).ToList();
    }

    public static List<string> CollectFrameworkRefs(string refAssemblyDir)
    {
        if (!Directory.Exists(refAssemblyDir)) return new List<string>();
        return Directory.EnumerateFiles(refAssemblyDir, "*.dll", SearchOption.TopDirectoryOnly)
                        .ToList();
    }
}
