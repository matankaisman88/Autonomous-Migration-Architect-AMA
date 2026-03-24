param(
    [string]$Sandbox = $env:SANDBOX,
    [string]$Domain = "",
    [int]$Lines = 10000,
    [int]$Seed = 42
)

$ErrorActionPreference = "Stop"

if (-not $Sandbox) {
    $Sandbox = "sample_data/kfar_supply"
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if ($Domain -and $PSBoundParameters.ContainsKey("Sandbox")) {
    throw "Use either -Domain or -Sandbox, not both."
}

if ($Domain) {
    Write-Host "==> [1/4] Generating $Domain domain sandbox ($Lines lines, seed $Seed)..."
    $generatedPath = python tools/generate_domain_data.py --domain $Domain --lines $Lines --seed $Seed --out-dir out --print-path-only
    if (-not $generatedPath) {
        throw "Generator did not return a sandbox path."
    }
    $Sandbox = $generatedPath.Trim()
    Write-Host "    Sandbox: $Sandbox"
} elseif ($Sandbox -eq "sample_data/kfar_supply") {
    Write-Host "==> [1/4] Generating Kfar Supply sample data..."
    python tools/generate_kfar_supply.py
} else {
    Write-Host "==> [1/4] Using sandbox $Sandbox (skipping generators)..."
}

if ($Sandbox -ne "sample_data/kfar_supply") {
    $candidate = $Sandbox
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
        $candidate = Join-Path $root $Sandbox
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
        throw "Sandbox directory not found: $Sandbox"
    }
    $Sandbox = (Resolve-Path -LiteralPath $candidate).Path
}

$ddlDir = Join-Path $Sandbox "ddl"
if (-not (Test-Path -LiteralPath $ddlDir -PathType Container)) {
    throw "Missing $ddlDir (incomplete or stale sandbox)."
}

$manifest = Join-Path $ddlDir "manifest.json"
$kfarManifest = Join-Path $ddlDir "kfar_manifest.json"
if (Test-Path -LiteralPath $manifest -PathType Leaf) {
    $ddlName = "manifest.json"
} elseif (Test-Path -LiteralPath $kfarManifest -PathType Leaf) {
    $ddlName = "kfar_manifest.json"
} else {
    throw "No manifest.json or kfar_manifest.json under $ddlDir"
}

if ($Sandbox -eq "sample_data/kfar_supply") {
    $sandboxRel = "sample_data/kfar_supply"
} else {
    $sandboxRel = [System.IO.Path]::GetRelativePath($root, $Sandbox).Replace("\", "/")
}

$ddlManifest = "$sandboxRel/ddl/$ddlName"

if ($Domain) {
    $report = "$sandboxRel/${Domain}_report.json"
    $jiraOut = "$sandboxRel/${Domain}_export_jira.csv"
    $confOut = "$sandboxRel/${Domain}_export_confluence.html"
} else {
    $report = "$sandboxRel/kfar_report.json"
    $jiraOut = "$sandboxRel/kfar_export_jira.csv"
    $confOut = "$sandboxRel/kfar_export_confluence.html"
}

$reportAbs = [System.IO.Path]::GetFullPath((Join-Path $root $report))
$jiraAbs = [System.IO.Path]::GetFullPath((Join-Path $root $jiraOut))
$confAbs = [System.IO.Path]::GetFullPath((Join-Path $root $confOut))

Write-Host "==> [2/4] Ingesting SQL logs (discovery mode)..."
ama-ingest run `
  --data-root . `
  --sql-logs "$sandboxRel/sql_logs/*.jsonl" `
  --ddl-manifest "$ddlManifest" `
  --glossary "$sandboxRel/glossary/*_glossary.json" `
  --glossary-dirty "$sandboxRel/glossary/*_glossary_dirty.json" `
  --comms-dir "$sandboxRel/comms" `
  --git-sql-roots "$sandboxRel/git_sql" `
  --target-schema dbo `
  --target-table orders `
  --discovery-mode --discovery-merge-all `
  --format json `
  -o "$report"

Write-Host "==> [3/4] Export plan (Jira CSV import)..."
ama-ingest export-plan --report "$report" --format jira --out "$jiraOut"

Write-Host "==> [4/4] Export plan (Confluence HTML)..."
ama-ingest export-plan --report "$report" --format confluence --out "$confOut"

Write-Host ""
Write-Host "Demo outputs (under sandbox, absolute paths):"
Write-Host "  Report JSON:          $reportAbs"
Write-Host "  Jira export:          $jiraAbs"
Write-Host "  Confluence export:    $confAbs"
Write-Host "  Dashboard (copy/paste): ama-dashboard --report-path `"$reportAbs`""
