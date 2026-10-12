using System.Text;
using System.Text.Json;
using Microsoft.Build.Locator;

namespace DotnetCodeAdvisor.RoslynHost;

internal static class Program
{
    private static async Task<int> Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);

        // MUST run before ANY Microsoft.CodeAnalysis.Workspaces.MSBuild type is
        // touched.  Registration happens in a separate non-inlined method so the
        // JIT cannot have resolved the MSBuild types before it runs.
        try
        {
            RegisterMsBuild();
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("[host] FATAL: " + ex.Message);
            return 2;
        }

        return await RunAsync(args).ConfigureAwait(false);
    }

    // Kept out of Main so that touching Roslyn types cannot precede registration.
    [System.Runtime.CompilerServices.MethodImpl(
        System.Runtime.CompilerServices.MethodImplOptions.NoInlining)]
    private static async Task<int> RunAsync(string[] args)
    {
        var host = new WorkspaceHost();
        var rpc = new Rpc(host);

        // --bench runs the load once, prints the measurement and exits.  It is
        // the go/no-go gate and must not need the MCP server.
        if (args.Length > 0 && args[0] == "--bench")
        {
            return await Bench.RunAsync(host, args).ConfigureAwait(false);
        }

        // --index runs the offline index pass (Indexer) and exits.
        if (args.Length > 1 && args[0] == "--index")
        {
            return await Indexer.RunAsync(host, args[1]).ConfigureAwait(false);
        }

        var stdin = Console.In;
        string? line;
        while ((line = await stdin.ReadLineAsync().ConfigureAwait(false)) is not null)
        {
            if (line.Length == 0) continue;
            string reply;
            try
            {
                reply = await rpc.DispatchAsync(line).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                reply = JsonSerializer.Serialize(new
                {
                    id = (int?)null,
                    ok = false,
                    error = new { code = "internal", message = ex.ToString() }
                });
            }
            Console.Out.Write(reply);
            Console.Out.Write('\n');
            Console.Out.Flush();
        }
        return 0;
    }

#if NET48
    // This host is compiled against MSBuild 17.14 (MSBuildPkgVersion) and runs
    // VS's MSBuild in-process, sharing one copy of System.Collections.Immutable
    // and friends.  VS 18's MSBuild needs newer ones than this host ships, and
    // fails in XMakeElements' type initializer with a FrozenSet MissingMethodException.
    private static void WarnIfNotContract(VisualStudioInstance vs)
    {
        if (vs.Version.Major != 17)
            Console.Error.WriteLine(
                $"[host] WARNING: MSBuild {vs.Version} is not 17.x; this host is built against 17.14 " +
                "and cannot load projects with it. Install VS 2022 Build Tools 17.14 " +
                "(scripts/provision-vm.ps1) and set [build] msbuild in config.toml to its MSBuild.exe.");
    }
#endif

    private static void RegisterMsBuild()
    {
        if (MSBuildLocator.IsRegistered) return;
#if NET48
        // .NET Framework host -> Visual Studio's MSBuild.  The Python side passes
        // the MSBuild the workspace build used (DOTNET_ADVISOR_MSBUILD_BIN), so loading
        // and building agree even with several VS installs of the same version;
        // without it, highest version wins.
        var instances = MSBuildLocator.QueryVisualStudioInstances().ToList();
        var pinned = (Environment.GetEnvironmentVariable("DOTNET_ADVISOR_MSBUILD_BIN") ?? "").Trim().TrimEnd('\\', '/');
        if (pinned.Length > 0)
        {
            var match = instances.FirstOrDefault(i => string.Equals(
                i.MSBuildPath.TrimEnd('\\', '/'), pinned.Replace('/', '\\'), StringComparison.OrdinalIgnoreCase));
            if (match != null)
            {
                Console.Error.WriteLine($"[host] MSBuild {match.Version} from {match.MSBuildPath} (pinned)");
                WarnIfNotContract(match);
                MSBuildLocator.RegisterInstance(match);
                return;
            }
            Console.Error.WriteLine($"[host] DOTNET_ADVISOR_MSBUILD_BIN={pinned} is not a Visual Studio instance; using the highest version");
        }
        var vs = instances.OrderByDescending(i => i.Version).FirstOrDefault()
                 ?? throw new InvalidOperationException(
                        "No Visual Studio MSBuild found. Install VS 2022 (or newer) with " +
                        "the .NET desktop + ASP.NET workloads on this machine.");
        Console.Error.WriteLine($"[host] MSBuild {vs.Version} from {vs.MSBuildPath}");
        WarnIfNotContract(vs);
        MSBuildLocator.RegisterInstance(vs);
#else
        var sdk = MSBuildLocator.RegisterDefaults();
        Console.Error.WriteLine($"[host] MSBuild {sdk.Version} from {sdk.MSBuildPath}");
#endif
    }
}
