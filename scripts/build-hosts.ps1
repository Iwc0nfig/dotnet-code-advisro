# Build both Roslyn hosts and repair the net472 BuildHost payload.
#
# Why the repair is needed
# ------------------------
# MSBuildWorkspace (Roslyn >= 4.9) always loads projects through an
# out-of-process "BuildHost".  For .NET Framework projects that is
# BuildHost-net472\Microsoft.CodeAnalysis.Workspaces.MSBuild.BuildHost.exe,
# which registers Visual Studio's MSBuild at run time.
#
# VS 17.14's Microsoft.Build.dll is compiled against
# System.Threading.Tasks.Extensions 4.2.4.0, but the NuGet payload ships 4.2.0.1
# and its app.config carries binding redirects for every MSBuild assembly except
# that one.  The local (older) copy wins the probe, ValueTask resolves to a
# different type identity, and the BuildHost dies with:
#
#   TypeLoadException: Method 'DisposeAsync' in type
#   'Microsoft.Build.Experimental.ProjectCache.ProjectCacheService' from assembly
#   'Microsoft.Build, Version=15.1.0.0' does not have an implementation.
#
# ...which surfaces to the caller only as "The BuildHost process is not
# responding", i.e. every legacy project silently fails to load.
#
# This script copies the VS-matched assembly into our own output folder and adds
# the missing binding redirect.  Nothing outside this repository is modified.

[CmdletBinding()]
param(
  [string]$Configuration = "Release",
  [switch]$SkipBuild
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$proj = Join-Path $root "roslyn-host\RoslynHost.csproj"

if (-not $SkipBuild) {
  Write-Host "==> building $proj ($Configuration)"
  & dotnet build $proj -c $Configuration --nologo -v minimal
  if ($LASTEXITCODE -ne 0) { throw "build failed" }

  # The page-code bridge build-workspace.py runs for every Website Project
  # (config.toml paths.markup_codegen).  net48 only: it hosts System.Web.
  $codegen = Join-Path $root "markup-codegen\MarkupCodegen.csproj"
  Write-Host "==> building $codegen ($Configuration)"
  & dotnet build $codegen -c $Configuration --nologo -v minimal
  if ($LASTEXITCODE -ne 0) { throw "markup-codegen build failed" }
}

function Get-VsMsBuildBin {
  $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
  if (Test-Path $vswhere) {
    $p = & $vswhere -latest -products * -requires Microsoft.Component.MSBuild `
                    -property installationPath 2>$null | Select-Object -First 1
    if ($p) {
      $bin = Join-Path $p "MSBuild\Current\Bin"
      if (Test-Path $bin) { return $bin }
    }
  }
  $fallback = Get-ChildItem "C:\Program Files\Microsoft Visual Studio\*\*\MSBuild\Current\Bin" `
                            -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($fallback) { return $fallback.FullName }
  return $null
}

$vsBin = Get-VsMsBuildBin
if (-not $vsBin) {
  Write-Warning "No Visual Studio MSBuild found; skipping BuildHost repair. Legacy (.NET Framework) projects will not load."
  exit 0
}
Write-Host "==> VS MSBuild: $vsBin"

$ASM = "System.Threading.Tasks.Extensions.dll"
$srcDll = Join-Path $vsBin $ASM
if (-not (Test-Path $srcDll)) { throw "$ASM not found in $vsBin" }
$srcVer = [System.Reflection.AssemblyName]::GetAssemblyName($srcDll).Version.ToString()

$hostDirs = Get-ChildItem (Join-Path $root "roslyn-host\bin\$Configuration") -Recurse `
                          -Directory -Filter "BuildHost-net472" -ErrorAction SilentlyContinue
if (-not $hostDirs) { Write-Warning "no BuildHost-net472 folders found"; exit 0 }

foreach ($d in $hostDirs) {
  $dstDll = Join-Path $d.FullName $ASM
  $had = if (Test-Path $dstDll) {
    [System.Reflection.AssemblyName]::GetAssemblyName($dstDll).Version.ToString()
  } else { "(absent)" }
  Copy-Item $srcDll $dstDll -Force
  Write-Host "  $($d.FullName)"
  Write-Host "     $ASM  $had -> $srcVer"

  $cfg = Join-Path $d.FullName "Microsoft.CodeAnalysis.Workspaces.MSBuild.BuildHost.exe.config"
  if (-not (Test-Path $cfg)) { Write-Warning "     no exe.config; redirect not added"; continue }

  [xml]$xml = Get-Content $cfg
  $ns = "urn:schemas-microsoft-com:asm.v1"
  $asmb = $xml.configuration.runtime.assemblyBinding
  if (-not $asmb) { Write-Warning "     no assemblyBinding section"; continue }

  $short = [IO.Path]::GetFileNameWithoutExtension($ASM)
  $existing = $asmb.dependentAssembly | Where-Object { $_.assemblyIdentity.name -eq $short }
  if ($existing) {
    $existing.bindingRedirect.oldVersion = "0.0.0.0-$srcVer"
    $existing.bindingRedirect.newVersion = $srcVer
    Write-Host "     binding redirect updated -> $srcVer"
  } else {
    $dep = $xml.CreateElement("dependentAssembly", $ns)
    $id  = $xml.CreateElement("assemblyIdentity", $ns)
    $id.SetAttribute("name", $short)
    $id.SetAttribute("publicKeyToken", "cc7b13ffcd2ddd51")
    $id.SetAttribute("culture", "neutral")
    $br = $xml.CreateElement("bindingRedirect", $ns)
    $br.SetAttribute("oldVersion", "0.0.0.0-$srcVer")
    $br.SetAttribute("newVersion", $srcVer)
    $dep.AppendChild($id) | Out-Null
    $dep.AppendChild($br) | Out-Null
    $asmb.AppendChild($dep) | Out-Null
    Write-Host "     binding redirect added -> $srcVer"
  }
  $xml.Save($cfg)
}

Write-Host "==> BuildHost repair complete"
