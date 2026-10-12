# Validation

License update: release 0.2.0 replaces MIT for new copies with a custom
monetization-restricted license. Package metadata and license contents were checked,
distributions rebuilt, and the source ZIP refreshed. The runtime verification below
was performed for release 0.1.0; the only runtime change in 0.2.0 is the reported
version number. The custom legal terms have not received legal review.

Validated on Windows with Python 3.13 and .NET SDK 9.0.318, on 2026-10-12.
All application inputs were newly authored fictional samples in temporary copies.
No company application source, indexes or personal attachment were included.

| Check | Result |
| --- | --- |
| Fresh isolated install | Passed using the declared dependencies, including MCP 2.3.0. |
| README quick start | Passed from a fresh source copy: structural index, smoke check and all 15 tools listed. |
| Installed-wheel regression suite | **80 passed, 1 skipped**, with compiler integration enabled. |
| Real MCP stdio connection | Initialization, listing all 15 tools, and a source read passed. |
| Optional roots and relative configuration | Passed: legacy-only, modern-only, arbitrary folder names and invalid-root rejection. |
| Source boundaries | Absolute paths, traversal, sibling-prefix paths and a Windows junction escape were rejected. |
| Read-only contract | All 15 query tools exercised under source-write guards; C# interactive-host mutation check passed. |
| Roslyn helpers | net48 and net8.0 builds passed with patched MSBuild 17.14.28. |
| ASP.NET page bridge | net48 build passed. |
| Disposable sample compiler index | Three projects compiled cleanly, including the Website Project; zero binding errors. |
| HTTP route analysis | One sample call, one endpoint, one exact route match; no unmatched calls. |
| Migration types | One same-name twin pair with identical public shape; synthetic tests also cover API-contract evidence. |
| Live compiler hosts | Both loaded their sample solutions and answered compiler-backed type queries. |
| Distribution builds | Wheel and source archive built; archive contents checked for private identifiers and generated data. |
| Public-source privacy scan | No findings for supplied private identifiers, personal paths, internal addresses, or credential patterns. |
| Original implementation | Hashes of all 58 copied implementation source files confirmed unchanged. |

The skipped test requires two filenames differing only in case, which the Windows
filesystem used here cannot represent. The ordinary source test run skips the
compiler test unless explicitly enabled; it was enabled for the installed-wheel run.

The helper build retains NuGet compatibility warning NU1701 for Microsoft.Build and
Microsoft.IO.Redist framework assets. Both compiler modes and live queries worked
in the tested environment. Other MSBuild/SDK combinations and Linux/macOS execution
were not verified locally; the included CI workflow covers portable Python tests
on Ubuntu and Windows with Python 3.12 and 3.13 when run on GitHub.

MSBuild 17.14.28 replaces the inherited 17.14.8 pin to address the published
[Microsoft advisory](https://github.com/advisories/GHSA-w3q9-fxm7-j8fq).

Generated helpers, application build outputs, indexes, logs and test caches were
removed from the delivered source folder. Local build distributions and temporary
validation workspaces remain outside that folder. Nothing was published to GitHub.

Repeat the source tests with `python -m pytest`; use `scripts/validate_compiler.py`
and the environment settings in [ADAPTING.md](ADAPTING.md) for the compiler check.
Run `scripts/check_public_release.py` before publishing; supply your own private
identifier patterns with repeated `--deny-pattern` arguments. Pattern checks are
an audit aid, not proof that every possible confidential detail is absent.
