#Requires -Version 5.1
# List recent GitHub Actions runs for daily.yml (debug: who triggers Telegram send).
# Needs: env GITHUB_PAT or scripts/.github_pat_local (same as Trigger-GitHubDailyWorkflow.ps1)
param(
    [string] $Pat,
    [string] $Owner = "dragonheart8787",
    [string] $Repo = "Corporate-Bond-Bot",
    [int] $PerPage = 25
)

$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$tokenFile = Join-Path $scriptDir ".github_pat_local"

function Test-PatPresent([string]$s) { return -not [string]::IsNullOrWhiteSpace($s) }
function Read-PatFromFile([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $raw = Get-Content -LiteralPath $path -Raw -Encoding UTF8
    if ($null -eq $raw) { return $null }
    $t = $raw.Trim().TrimStart([char]0xFEFF).Trim()
    foreach ($line in ($t -split "`r?`n")) {
        $line = $line.Trim()
        if ($line.Length -eq 0 -or $line.StartsWith("#")) { continue }
        $line = $line.Trim('"').Trim("'")
        if ($line -match '^(?i)Bearer\s+') { $line = $line -replace '^(?i)Bearer\s+', '' }
        if (Test-PatPresent $line) { return $line.Trim() }
    }
    return $null
}

if (-not (Test-PatPresent $Pat)) { $Pat = $env:GITHUB_PAT }
if (-not (Test-PatPresent $Pat)) { $Pat = Read-PatFromFile $tokenFile }
if (-not (Test-PatPresent $Pat)) {
    Write-Host "Set GITHUB_PAT or create scripts\.github_pat_local"
    exit 1
}

$headers = @{
    Accept                 = "application/vnd.github+json"
    Authorization        = "Bearer $Pat"
    "X-GitHub-Api-Version" = "2022-11-28"
}
$uri = "https://api.github.com/repos/$Owner/$Repo/actions/workflows/daily.yml/runs?per_page=$PerPage"
$data = Invoke-RestMethod -Uri $uri -Headers $headers -Method Get

Write-Host ""
Write-Host "Recent runs: $Owner/$Repo workflow daily.yml"
Write-Host "If you see many workflow_dispatch close together -> PAT/Task Scheduler/n8n."
Write-Host "schedule should appear ~once per day at UTC 14:xx."
Write-Host ("{0,-22} {1,-18} {2,-12} {3}" -f "created_at(UTC)", "event", "conclusion", "html_url")
Write-Host ("-" * 120)
foreach ($r in $data.workflow_runs) {
    Write-Host ("{0,-22} {1,-18} {2,-12} {3}" -f $r.created_at, $r.event, $r.conclusion, $r.html_url)
}
Write-Host ""
