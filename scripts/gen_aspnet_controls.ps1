# Build .index/aspnet_controls.json by reflecting over the .NET Framework
# reference assemblies.  rule 4: never hand-maintain this list.
#
# System.Web.dll alone is not enough: ScriptManager, UpdatePanel, Timer and
# UpdateProgress live in System.Web.UI inside System.Web.Extensions.dll, and
# they account for most asp: tags that would otherwise fall back to
# System.Web.UI.Control.
# Must run under Windows PowerShell 5.1 (.NET Framework).
param(
  [string]$RefDir = "C:\Program Files (x86)\Reference Assemblies\Microsoft\Framework\.NETFramework\v4.7.2",
  [string]$OutFile = "$PSScriptRoot\..\.index\aspnet_controls.json"
)
$ErrorActionPreference = "Stop"

$assemblies = @("System.Web.dll", "System.Web.Extensions.dll", "System.Web.Mobile.dll")
$web = @{}; $html = @{}; $ui = @{}; $loaded = @()

foreach ($name in $assemblies) {
  $dll = Join-Path $RefDir $name
  if (-not (Test-Path $dll)) { continue }
  try { $asm = [System.Reflection.Assembly]::LoadFrom($dll) } catch { continue }
  $loaded += $dll
  foreach ($t in $asm.GetExportedTypes()) {
    switch ($t.Namespace) {
      "System.Web.UI.WebControls"  { if (-not $web.ContainsKey($t.Name))  { $web[$t.Name]  = $t.FullName } }
      "System.Web.UI.HtmlControls" { if (-not $html.ContainsKey($t.Name)) { $html[$t.Name] = $t.FullName } }
      "System.Web.UI"              { if (-not $ui.ContainsKey($t.Name))   { $ui[$t.Name]   = $t.FullName } }
    }
  }
}
if ($loaded.Count -eq 0) { throw "no reference assemblies loaded from $RefDir" }

$out = [ordered]@{
  sources       = $loaded
  generated_utc = (Get-Date).ToUniversalTime().ToString("o")
  webcontrols   = $web
  htmlcontrols  = $html
  uicontrols    = $ui
}
$dir = Split-Path -Parent $OutFile
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
$out | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $OutFile
Write-Host "wrote $OutFile"
Write-Host "  sources:      $($loaded.Count)"
Write-Host "  WebControls:  $($web.Count)"
Write-Host "  HtmlControls: $($html.Count)"
Write-Host "  System.Web.UI:$($ui.Count)"
