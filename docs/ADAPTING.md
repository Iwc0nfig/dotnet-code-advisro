# Adapt the advisor to your codebase

## 1. Choose source roots and analysis depth

Use a common `workspaces` directory containing your source roots. Folder names do
not matter. For a modern-only repository, set `net8_root` and omit `net48_root`,
`net48_solution` and `website_root`. For legacy-only repositories, do the reverse.
The roots must exist, lie under `workspaces`, and not overlap. Keep `index_dir`
separate from both source roots.

Copy `config.example.toml` to `config.toml`, then adjust `[paths]`. For example:

```toml
[paths]
workspaces = "C:/dev/workspaces"
net8_root = "C:/dev/workspaces/Payments"
net8_solution = "C:/dev/workspaces/Payments/Payments.sln"
index_dir = ".index"
```

All relative paths, including helper executables and framework directories, are
relative to the configuration file. Select it with `--config` or
`DOTNET_ADVISOR_CONFIG`. An explicit `--config` takes precedence. Automatic lookup
checks the current directory and the advisor source repository, without searching
parent deployments. Do not commit local configuration or indexed application source.

The `net8` label identifies the modern side and its net8.0 helper host. Project
frameworks come from project files and build inputs; the label alone does not
guarantee support for every SDK or target framework. Use the project's required SDK
and validate loading through `index_status`.

## 2. Configure exclusions

`[index]` supports `max_file_bytes`, `exclude_dirs`, `exclude_suffixes` and
`vendor_js_pattern`. Arrays replace the defaults; retain build/cache exclusions
when adding your own. Directory names and suffixes match case-insensitively.
An empty vendor regex matches every JavaScript name; use `(?!)` to exclude none.

Inventory, project discovery and text search honor these settings. Indexed vendor
JavaScript and oversized files are counted as skipped; pruned directories are not
walked. `index_status` reports actual indexed counts and limitations.

## 3. Start with structural analysis

```powershell
.\.venv\Scripts\python.exe scripts/reindex.py --config config.toml --no-roslyn
.\.venv\Scripts\python.exe -m dotnet_code_advisor --config config.toml --no-roslyn
```

If `website_root` is configured, the indexer expects a reflected ASP.NET control
table. The explicit `--allow-missing-controls` option lets you inspect a workspace
without that table, with control-type fallbacks reported in the results. Do not
interpret those fallbacks as accurate control-type resolution.

No application restore/build is required for this mode. Source reads are restricted
to configured roots after resolving paths and links. Avoid changing symlinks or
junctions while indexing or serving a workspace.

## 4. Add WebForms support

Set `website_root` to the primary Website Project or Web Application directory under
`net48_root`. The graph reads its `Web.config` control registrations and resolves
application-relative paths there. Page-local `<%@ Register %>` declarations are
also read. Additional websites have compiler coverage when discovered in solutions,
but graph-wide control registration and `~/` resolution use this primary root.

Generate real framework-control types with Windows PowerShell 5.1:

```powershell
powershell -File scripts/gen_aspnet_controls.ps1 `
  -RefDir "C:/Program Files (x86)/Reference Assemblies/Microsoft/Framework/.NETFramework/v4.8" `
  -OutFile ".index/aspnet_controls.json"
```

Change `-OutFile` if your configured index directory differs. Custom control types
come from registrations and source declarations; do not manually create designer
files in the application. Graph-generated fields are kept in the index directory.
Controls inside data-bound templates are overapproximated in the graph; generated
ASP.NET compiler code provides stronger evidence when available.

## 5. Build compiler helpers

The helper project targets net48 and net8.0. Its legacy host pins Roslyn 4.8 for
in-process MSBuild and MSBuild packages 17.14.28; use a compatible Visual Studio 2022
17.14 installation with MSBuild and ASP.NET/web build tools. Its modern host pins
Roslyn 4.14. Install the .NET SDK and framework targeting packs your projects need.
Older framework reference assemblies may be downloaded by the workspace builder;
`--no-fetch-refasm` disables that download.

```powershell
powershell -File scripts/build-hosts.ps1
```

This builds the Roslyn hosts and net48 ASP.NET page bridge, with a BuildHost payload
repair where the installed MSBuild requires it. Set these optional paths afterward:

```toml
roslyn_host_net48 = "roslyn-host/bin/Release/net48/RoslynHost.exe"
roslyn_host_net8 = "roslyn-host/bin/Release/net8.0/RoslynHost.exe"
markup_codegen = "markup-codegen/bin/Release/net48/MarkupCodegen.exe"
```

For modern-only use, build `roslyn-host/RoslynHost.csproj` with
`dotnet build -c Release -f net8.0`, configure just `roslyn_host_net8`, and omit the
page bridge. `netfx_ref_assemblies` optionally supplies the legacy framework-reference
directory. `[build] msbuild` selects a compatible MSBuild executable; the helper
environment uses `DOTNET_ADVISOR_MSBUILD_BIN` to pin that installation.

## 6. Build a dedicated application copy, then index

Building can execute project build targets and writes `bin`, `obj`, and Website
`Bin` outputs. Use a trusted, disposable copy of the application. Configure the
advisor to that copy before running:

```powershell
.\.venv\Scripts\python.exe scripts/build-workspace.py --config config.toml
.\.venv\Scripts\python.exe scripts/reindex.py --config config.toml
```

The build discovers projects and Website Projects, restores/builds in dependency
order, generates ASP.NET page code, and records failures in an index-local manifest.
Compiler indexing follows that manifest, including project-reference substitutions.
Substitutions are analysis inputs, not edits to application project files.

A primary website not listed in a solution can still be structurally indexed, but
the workspace build needs a solution Website Project entry for exact page codegen.
Add that entry in your dedicated copy, or retain the reported graph fallback.

Run `scripts/validate_compiler.py` after building the helpers to check the fictional
samples in a fresh temporary copy. It never builds the checked-in examples in place.

To retain the sample index and run the opt-in query test:

```powershell
.\.venv\Scripts\python.exe scripts/validate_compiler.py --work-dir C:/temp/advisor-check
$env:DOTNET_ADVISOR_CONFIG = "C:/temp/advisor-check/config.toml"
$env:DOTNET_ADVISOR_ROSLYN = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_compiler_integration.py
```

Use a fresh external work directory. The compiler test expects these fictional
samples; do not enable it against a private application index.

## 7. Verify coverage and refresh

Call `index_status`; inspect project build outcomes, binding errors, host states,
skipped files, and compiler-index coverage. `clean: false` means unresolved uses may
be absent. A missing host is `unavailable`, not proof that a symbol has no uses.
Unmatched HTTP routes and URLs computed at runtime need separate inspection.

After source updates, stop the query server, rebuild the workspace copy if using
compiler analysis, rerun indexing, then restart the server. Index rebuilds replace
the existing database. Do not rebuild while the query server is serving it.

Troubleshooting:

| Symptom | Check |
| --- | --- |
| Configuration fails | Existing roots, non-overlap, solution paths and relative-path base. |
| Empty structural results | Configured roots, exclusions and the selected index directory. |
| Control fallback types | Reflected table, primary website, registrations and custom-control source. |
| Compiler unavailable | Helper path, installed SDK/MSBuild, configured solution and `--no-roslyn`. |
| Compiler binding errors | Restore/build results, framework packs and website `Bin` inputs. |
| Source file rejected | Whether its resolved path stays within a configured root. |
| A reference is missing | Evidence layer, exclusions, build timestamp and project binding errors. |

## 8. Extend the advisor

Add synthetic regression fixtures before changing parser/resolution behavior.
Keep tool schemas and evidence distinctions stable. New read-only tools must be
added deliberately to `EXPECTED_TOOLS` and tested for source-write prevention.
Supporting another language needs a parser, symbol/reference extraction, confidence
rules and tests; compiler-exact claims also need an analysis backend for that language.

Before publishing your fork, scan source, filenames and package contents for private
identifiers. Keep configuration, indexes, logs, build artifacts and application
snapshots outside commits and distributions.
