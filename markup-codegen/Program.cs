using System.CodeDom.Compiler;
using System.Collections;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Web.Compilation;

namespace DotnetCodeAdvisor.MarkupCodegen;

/// <summary>
/// Asks ASP.NET for the C# it generates for every markup file of a Website
/// Project, so the Roslyn index compiles what runs instead of an approximation.
///
///   usage: MarkupCodegen --site &lt;physical root&gt; --out &lt;dir&gt;
///
/// For each .aspx/.ascx/.master/.asax/.ashx/.asmx it writes, under --out:
///   &lt;rel&gt;.g.cs       the generated classes: the designer partial with the
///                     real control fields, and the ASP.* class holding every
///                     &lt;%= %&gt; / &lt;%# %&gt; / &lt;% %&gt; nugget as compilable C#
///   &lt;rel&gt;.map.json   column map for those nuggets (see Remap)
/// and codegen.json: per-file status, the exact assembly list ASP.NET compiles
/// against, and the compiler options from Web.config.
///
/// Nothing is written under the site.  ASP.NET's own scratch output goes to its
/// usual "Temporary ASP.NET Files" directory, as it would for a first request.
/// </summary>
internal static class Program
{
    private static readonly string[] MarkupExtensions =
        { ".aspx", ".ascx", ".master", ".asax", ".ashx", ".asmx" };

    // Directory names ASP.NET never serves or compiles markup from.
    private static readonly HashSet<string> SkipDirs = new(StringComparer.OrdinalIgnoreCase)
        { "bin", "obj", "App_Data", "PrecompiledWeb", "node_modules", "packages" };

    private static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        string? site = null, outDir = null;
        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--site": site = i + 1 < args.Length ? args[++i] : null; break;
                case "--out": outDir = i + 1 < args.Length ? args[++i] : null; break;
            }
        }
        if (site is null || outDir is null)
        {
            Console.Error.WriteLine("usage: MarkupCodegen --site <physical root> --out <dir>");
            return 2;
        }
        site = Path.GetFullPath(site).TrimEnd('\\', '/');
        outDir = Path.GetFullPath(outDir);
        if (!Directory.Exists(site))
        {
            Console.Error.WriteLine($"[codegen] site not found: {site}");
            return 2;
        }
        if (IsUnder(outDir, site))
        {
            Console.Error.WriteLine("[codegen] --out must not be inside the site");
            return 2;
        }
        ResetOutDir(outDir);

        // The provider Type and CompilerParameters come back across the hosting
        // AppDomain boundary, so THIS domain must be able to load the site's Bin
        // assemblies (the Roslyn CodeDom provider lives there, not in the GAC).
        var bin = Path.Combine(site, "Bin");
        AppDomain.CurrentDomain.AssemblyResolve += (_, e) =>
        {
            var f = Path.Combine(bin, new System.Reflection.AssemblyName(e.Name).Name + ".dll");
            return File.Exists(f) ? System.Reflection.Assembly.LoadFrom(f) : null;
        };

        var files = EnumerateMarkup(site).ToList();
        Console.Error.WriteLine($"[codegen] {site}: {files.Count} markup files");

        var cbm = new ClientBuildManager("/", site);
        var codeGenDir = cbm.CodeGenDir;
        var refs = new SortedDictionary<string, RefEntry>(StringComparer.OrdinalIgnoreCase);
        var results = new List<FileResult>();
        string? compilerOptions = null;
        var sourceCache = new Dictionary<string, string[]>(StringComparer.OrdinalIgnoreCase);

        foreach (var rel in files)
        {
            var vp = "/" + rel.Replace('\\', '/');
            var r = new FileResult { Path = rel.Replace('\\', '/') };
            try
            {
                var ccu = cbm.GenerateCodeCompileUnit(vp, out _, out var cp, out var pragmas);
                if (ccu is null)
                {
                    r.Status = "no_code";
                    results.Add(r);
                    continue;
                }
                var sw = new StringWriter();
                new Microsoft.CSharp.CSharpCodeProvider().GenerateCodeFromCompileUnit(
                    ccu, sw, new CodeGeneratorOptions { BlankLinesBetweenMembers = false, IndentString = "    " });

                var remap = Remap(sw.ToString(), pragmas, sourceCache);
                var outFile = Path.Combine(outDir, rel + ".g.cs");
                Directory.CreateDirectory(Path.GetDirectoryName(outFile)!);
                File.WriteAllText(outFile, remap.Text, new UTF8Encoding(false));
                File.WriteAllText(Path.Combine(outDir, rel + ".map.json"),
                    JsonSerializer.Serialize(remap.Map, JsonOpts), new UTF8Encoding(false));

                r.Status = "ok";
                r.Pragmas = pragmas?.Count ?? 0;
                r.Mapped = remap.Map.Count;
                r.Unmapped = remap.Unmapped;
                if (cp is not null)
                {
                    compilerOptions ??= cp.CompilerOptions;
                    foreach (var a in cp.ReferencedAssemblies)
                        AddRef(refs, (string)a!, codeGenDir, bin, outDir);
                }
            }
            catch (Exception ex)
            {
                r.Status = "failed";
                r.Error = ex.GetType().Name + ": " + Flatten(ex.Message);
            }
            results.Add(r);
            if (results.Count % 50 == 0)
                Console.Error.WriteLine($"[codegen] {results.Count}/{files.Count}");
        }

        var summary = new
        {
            site = site.Replace('\\', '/'),
            compiler_options = compilerOptions,
            files_total = results.Count,
            files_ok = results.Count(x => x.Status == "ok"),
            files_failed = results.Count(x => x.Status == "failed"),
            files_no_code = results.Count(x => x.Status == "no_code"),
            references = refs.Values,
            files = results,
        };
        File.WriteAllText(Path.Combine(outDir, "codegen.json"),
            JsonSerializer.Serialize(summary, JsonOpts), new UTF8Encoding(false));
        Console.Error.WriteLine(
            $"[codegen] ok={summary.files_ok} failed={summary.files_failed} "
            + $"no_code={summary.files_no_code} refs={refs.Count}");
        return 0;
    }

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        WriteIndented = true,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
    };

    private static readonly Regex LineDirective =
        new(@"^(?<indent>\s*)#line (?<n>\d+) ""(?<file>[^""]*)""\s*$", RegexOptions.Compiled);

    /// <summary>
    /// In design-time code generation ASP.NET numbers its #line pragmas 1..N and
    /// returns the real positions in a side table (LinePragmaCodeInfo).  Rewrite
    /// each directive to the real markup line, so any Roslyn location maps to the
    /// right .aspx line, and record where each nugget starts on its generated
    /// line, so the column can be mapped too.  C# 7.3 (what Web.config compiles
    /// with) has no column-aware #line, hence the side map.
    /// </summary>
    private static RemapResult Remap(string text, IDictionary? pragmas,
                                     Dictionary<string, string[]> cache)
    {
        var lines = text.Replace("\r\n", "\n").Split('\n');
        var map = new List<MapEntry>();
        var unmapped = 0;
        for (var i = 0; i < lines.Length; i++)
        {
            var m = LineDirective.Match(lines[i]);
            if (!m.Success) continue;
            var id = int.Parse(m.Groups["n"].Value);
            var info = pragmas?[id];
            if (info is null) { unmapped++; continue; }

            var t = info.GetType();
            int Get(string name) => (int)t.GetProperty(name)!.GetValue(info)!;
            var startLine = Get("StartLine");
            var startCol = Get("StartColumn");
            var length = Get("CodeLength");
            var nugget = (bool)t.GetProperty("IsCodeNugget")!.GetValue(info)!;
            var file = m.Groups["file"].Value;

            lines[i] = $"{m.Groups["indent"].Value}#line {startLine} \"{file}\"";

            // The code follows on the next line, copied verbatim from the markup.
            if (i + 1 >= lines.Length) { unmapped++; continue; }
            var first = FirstCodeLine(file, startLine, startCol, length, cache);
            var genLine = lines[i + 1];
            var genCol = string.IsNullOrWhiteSpace(first) ? -1 : genLine.IndexOf(first, StringComparison.Ordinal);
            if (genCol < 0 && !string.IsNullOrWhiteSpace(first))
                genCol = genLine.IndexOf(first.Trim(), StringComparison.Ordinal) is var k and >= 0
                    ? k - (first.Length - first.TrimStart().Length)
                    : -1;
            map.Add(new MapEntry
            {
                GenLine = i + 2,               // 1-based line of the code in the output
                GenCol = genCol < 0 ? 0 : genCol + 1,
                SrcLine = startLine,
                SrcCol = startCol,
                Length = length,
                Nugget = nugget,
            });
            if (genCol < 0) unmapped++;
        }
        return new RemapResult { Text = string.Join("\r\n", lines), Map = map, Unmapped = unmapped };
    }

    private static string FirstCodeLine(string file, int line, int col, int length,
                                        Dictionary<string, string[]> cache)
    {
        if (!cache.TryGetValue(file, out var src))
        {
            src = File.Exists(file)
                ? File.ReadAllText(file).Replace("\r\n", "\n").Split('\n')
                : Array.Empty<string>();
            cache[file] = src;
        }
        if (line < 1 || line > src.Length) return "";
        var s = src[line - 1];
        if (col < 1 || col > s.Length + 1) return "";
        var rest = s.Substring(col - 1);
        // CodeLength -1 means "to the end of the file": an .ashx/.asmx whose class
        // is written inline after the directive.
        return length >= 0 && rest.Length > length ? rest.Substring(0, length) : rest;
    }

    private static void AddRef(SortedDictionary<string, RefEntry> refs, string path,
                               string codeGenDir, string bin, string outDir)
    {
        var name = Path.GetFileName(path);
        RefEntry e;
        if (IsUnder(path, codeGenDir))
        {
            if (name.StartsWith("App_Code.", StringComparison.OrdinalIgnoreCase)
                || name.StartsWith("App_Web_", StringComparison.OrdinalIgnoreCase))
            {
                // Built from sources the index compiles directly; referencing the
                // binary as well would define every type twice.
                e = new RefEntry { Name = name, Kind = "excluded_from_source", Path = null };
            }
            else if (path.IndexOf(@"\assembly\dl3\", StringComparison.OrdinalIgnoreCase) >= 0
                     && File.Exists(Path.Combine(bin, name)))
            {
                // A shadow copy of a Bin assembly: point at the stable original.
                e = new RefEntry { Name = name, Kind = "bin", Path = Path.Combine(bin, name) };
            }
            else
            {
                // App_GlobalResources, App_Browsers: generated with no source on
                // disk.  Copied, because the codegen dir is ASP.NET's scratch space.
                var dst = Path.Combine(outDir, "_refs", StableGeneratedName(name));
                Directory.CreateDirectory(Path.GetDirectoryName(dst)!);
                File.Copy(path, dst, overwrite: true);
                e = new RefEntry { Name = name, Kind = "generated", Path = dst };
            }
        }
        else if (path.IndexOf(@"\Reference Assemblies\", StringComparison.OrdinalIgnoreCase) >= 0)
            e = new RefEntry { Name = name, Kind = "framework", Path = path };
        else if (path.IndexOf(@"\assembly\GAC", StringComparison.OrdinalIgnoreCase) >= 0)
            e = new RefEntry { Name = name, Kind = "gac", Path = path };
        else if (IsUnder(path, bin))
            e = new RefEntry { Name = name, Kind = "bin", Path = path };
        else
            e = new RefEntry { Name = name, Kind = "other", Path = path };

        var key = StableGeneratedName(name);
        if (!refs.ContainsKey(key)) refs[key] = e;
    }

    // "App_GlobalResources.vonai3ui.dll" -> "App_GlobalResources.dll": the random
    // suffix changes every run and would make the output non-deterministic.
    private static string StableGeneratedName(string name)
    {
        var parts = name.Split('.');
        return parts.Length == 3 && parts[0].StartsWith("App_", StringComparison.OrdinalIgnoreCase)
            ? parts[0] + "." + parts[2]
            : name;
    }

    private static IEnumerable<string> EnumerateMarkup(string site)
    {
        var stack = new Stack<string>();
        stack.Push(site);
        var found = new List<string>();
        while (stack.Count > 0)
        {
            var dir = stack.Pop();
            foreach (var d in Directory.EnumerateDirectories(dir))
            {
                var n = Path.GetFileName(d);
                if (SkipDirs.Contains(n) || n.EndsWith("-Release", StringComparison.OrdinalIgnoreCase))
                    continue;
                stack.Push(d);
            }
            foreach (var f in Directory.EnumerateFiles(dir))
                if (MarkupExtensions.Contains(Path.GetExtension(f), StringComparer.OrdinalIgnoreCase))
                    found.Add(f.Substring(site.Length + 1));
        }
        found.Sort(StringComparer.Ordinal);
        return found;
    }

    private static void ResetOutDir(string outDir)
    {
        if (Directory.Exists(outDir))
        {
            var ours = File.Exists(Path.Combine(outDir, "codegen.json"))
                       || !Directory.EnumerateFileSystemEntries(outDir).Any();
            if (!ours)
                throw new InvalidOperationException(
                    $"refusing to clear {outDir}: it is not empty and has no codegen.json");
            // Empty it rather than delete it: Windows refuses to delete a
            // directory that any process has as its working directory.
            foreach (var d in Directory.EnumerateDirectories(outDir)) Directory.Delete(d, recursive: true);
            foreach (var f in Directory.EnumerateFiles(outDir)) File.Delete(f);
        }
        Directory.CreateDirectory(outDir);
    }

    private static bool IsUnder(string path, string dir) =>
        Path.GetFullPath(path).StartsWith(Path.GetFullPath(dir).TrimEnd('\\') + "\\",
                                          StringComparison.OrdinalIgnoreCase);

    private static string Flatten(string s) => s.Replace("\r", " ").Replace("\n", " ");

    private sealed class RemapResult
    {
        public string Text { get; set; } = "";
        public List<MapEntry> Map { get; set; } = new();
        public int Unmapped { get; set; }
    }

    private sealed class MapEntry
    {
        public int GenLine { get; set; }
        public int GenCol { get; set; }
        public int SrcLine { get; set; }
        public int SrcCol { get; set; }
        public int Length { get; set; }
        public bool Nugget { get; set; }
    }

    private sealed class RefEntry
    {
        public string Name { get; set; } = "";
        public string Kind { get; set; } = "";
        public string? Path { get; set; }
    }

    private sealed class FileResult
    {
        public string Path { get; set; } = "";
        public string Status { get; set; } = "";
        public string? Error { get; set; }
        public int Pragmas { get; set; }
        public int Mapped { get; set; }
        public int Unmapped { get; set; }
    }
}
