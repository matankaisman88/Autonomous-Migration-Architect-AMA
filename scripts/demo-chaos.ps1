param(
    [ValidateSet("sqlserver", "oracle", "db2", "multi-source", "extreme-chaos")]
    [string]$Target = "oracle",
    [int]$Lines = 200000,
    [int]$Scale = 1000,
    [int]$JoinWidth = 3,
    [int]$SelectColumns = 8,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

Set-Location (Resolve-Path "$PSScriptRoot\..")

function Ensure-Dirs {
    New-Item -ItemType Directory -Force -Path "chaos_data\sql_logs" | Out-Null
    New-Item -ItemType Directory -Force -Path "chaos_data\ddl" | Out-Null
    New-Item -ItemType Directory -Force -Path "out" | Out-Null
}

function Generate-Dialect([string]$Dialect, [int]$Rows, [int]$TableScale) {
    Write-Host ""
    Write-Host "[1/2][$Dialect] Generating chaos data (lines=$Rows, scale=$TableScale, joins=$JoinWidth, cols=$SelectColumns)..."
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    & $Python "tools/generate_extreme_chaos.py" `
        "--source-dialect" $Dialect `
        "--scale" $TableScale `
        "--lines" $Rows `
        "--join-width" $JoinWidth `
        "--select-columns" $SelectColumns `
        "--out" "chaos_data/sql_logs/extreme_${Dialect}.jsonl" `
        "--ddl-out" "chaos_data/ddl/extreme_${Dialect}_ddl.sql" `
        "--manifest-out" "chaos_data/ddl/extreme_${Dialect}_manifest.json"
    $sw.Stop()
    Write-Host "[1/2][$Dialect] Generation done in $([int]$sw.Elapsed.TotalSeconds)s."
}

function Build-Report([string]$Dialect) {
    Write-Host "[2/2][$Dialect] Building AMA report (this can take a while on large files)..."
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    
    $env:AMA_DDL_MANIFEST_PATH = "chaos_data/ddl/extreme_${Dialect}_manifest.json"
    $env:AMA_SQL_LOGS_GLOB = "chaos_data/sql_logs/extreme_${Dialect}.jsonl"
    $env:AMA_DISCOVERY_MERGE_ALL = "true"
    $env:AMA_SQL_PARSE_MODE = "regex"
    
    & $Python "-m" "ama.cli" "run" `
        "--discovery-mode" `
        "--skip-vectors" `
        "--format" "json" `
        "--out-file" "out/${Dialect}_report.json"
        
    Remove-Item Env:AMA_SQL_PARSE_MODE -ErrorAction SilentlyContinue
    Remove-Item Env:AMA_DDL_MANIFEST_PATH -ErrorAction SilentlyContinue
    Remove-Item Env:AMA_SQL_LOGS_GLOB -ErrorAction SilentlyContinue
    Remove-Item Env:AMA_DISCOVERY_MERGE_ALL -ErrorAction SilentlyContinue
    $sw.Stop()
    Write-Host "[2/2][$Dialect] Report done in $([int]$sw.Elapsed.TotalSeconds)s."
}

Ensure-Dirs

switch ($Target) {
    "sqlserver" {
        Generate-Dialect "sqlserver" $Lines $Scale
        Build-Report "sqlserver"
    }
    "oracle" {
        Generate-Dialect "oracle" $Lines $Scale
        Build-Report "oracle"
    }
    "db2" {
        Generate-Dialect "db2" $Lines $Scale
        Build-Report "db2"
    }
    "multi-source" {
        foreach ($d in @("sqlserver", "oracle", "db2")) {
            Generate-Dialect $d $Lines $Scale
            Build-Report $d
        }
    }
    "extreme-chaos" {
        Write-Host ""
        Write-Host "[1/2][sqlserver] Generating EXTREME chaos data (lines=1000000, scale=1000, joins=10, cols=24)..."
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        & $Python "tools/generate_extreme_chaos.py" `
            "--source-dialect" "sqlserver" `
            "--scale" 1000 `
            "--lines" 1000000 `
            "--join-width" 10 `
            "--select-columns" 24 `
            "--out" "chaos_data/sql_logs/extreme_sqlserver.jsonl" `
            "--ddl-out" "chaos_data/ddl/extreme_sqlserver_ddl.sql" `
            "--manifest-out" "chaos_data/ddl/extreme_sqlserver_manifest.json"
        $sw.Stop()
        Write-Host "[1/2][sqlserver] Generation done in $([int]$sw.Elapsed.TotalSeconds)s."
        
        Write-Host "[2/2][sqlserver] Building EXTREME AMA report..."
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $env:AMA_DDL_MANIFEST_PATH = "chaos_data/ddl/extreme_sqlserver_manifest.json"
        $env:AMA_SQL_LOGS_GLOB = "chaos_data/sql_logs/extreme_sqlserver.jsonl"
        $env:AMA_DISCOVERY_MERGE_ALL = "true"
        $env:AMA_SQL_PARSE_MODE = "regex"
        
        & $Python "-m" "ama.cli" "run" `
            "--discovery-mode" `
            "--skip-vectors" `
            "--format" "json" `
            "--out-file" "out/extreme_1m_sqlserver_report.json"
            
        Remove-Item Env:AMA_SQL_PARSE_MODE -ErrorAction SilentlyContinue
        Remove-Item Env:AMA_DDL_MANIFEST_PATH -ErrorAction SilentlyContinue
        Remove-Item Env:AMA_SQL_LOGS_GLOB -ErrorAction SilentlyContinue
        Remove-Item Env:AMA_DISCOVERY_MERGE_ALL -ErrorAction SilentlyContinue
        $sw.Stop()
        Write-Host "[2/2][sqlserver] Report done in $([int]$sw.Elapsed.TotalSeconds)s."
    }
}

Write-Host ""
Write-Host "Done. Generated artifacts under:"
Write-Host "  chaos_data/sql_logs"
Write-Host "  chaos_data/ddl"
Write-Host "  out/"