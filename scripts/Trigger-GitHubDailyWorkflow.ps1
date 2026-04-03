#Requires -Version 5.1
# Trigger GitHub Actions workflow_dispatch for daily.yml (no GitHub cron needed).
# Token: env GITHUB_PAT, or one-line file .github_pat_local next to this script (gitignored).
# Task Scheduler: run powershell.exe -NoProfile -ExecutionPolicy Bypass -File "...\Trigger-GitHubDailyWorkflow.ps1"
#                 Set "Start in" to the scripts folder so .github_pat_local is found.
param(
    [string] $Pat,
    [string] $Owner = "dragonheart8787",
    [string] $Repo = "Corporate-Bond-Bot",
    [string] $Branch = "auto",
    [ValidateSet("all", "send", "fetch")]
    [string] $Mode = "all"
)

$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot
if (-not $scriptDir) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$tokenFile = Join-Path $scriptDir ".github_pat_local"

function Test-PatPresent([string]$s) {
    return -not [string]::IsNullOrWhiteSpace($s)
}

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
if (-not (Test-PatPresent $Pat)) {
    $Pat = Read-PatFromFile $tokenFile
    if (Test-PatPresent $Pat) { Write-Host "[info] Loaded token from .github_pat_local" }
}

if (-not (Test-PatPresent $Pat)) {
    $envHas = Test-PatPresent $env:GITHUB_PAT
    $fileOk = Test-Path -LiteralPath $tokenFile
    $fileLen = if ($fileOk) { (Get-Item -LiteralPath $tokenFile).Length } else { 0 }
    Write-Host "[diag] scriptDir: $scriptDir"
    Write-Host "[diag] GITHUB_PAT env has value: $envHas"
    Write-Host "[diag] token file exists: $fileOk  path: $tokenFile  size(bytes): $fileLen"
    Write-Error "No GitHub PAT. Set user env GITHUB_PAT (new shell) or put token in scripts\.github_pat_local (one line, no quotes). GitHub: classic PAT with repo + workflow scope."
    exit 1
}

$headers = @{
    Accept                   = "application/vnd.github+json"
    Authorization            = "Bearer $Pat"
    "X-GitHub-Api-Version"   = "2022-11-28"
}

if ($Branch -eq "auto") {
    $repoUri = "https://api.github.com/repos/$Owner/$Repo"
    $info = Invoke-RestMethod -Uri $repoUri -Headers $headers -Method Get
    $Branch = $info.default_branch
    Write-Host "[info] Using default branch: $Branch"
}

$dispatchUri = "https://api.github.com/repos/$Owner/$Repo/actions/workflows/daily.yml/dispatches"
# -Depth 避免巢狀 inputs 在部分 PS 版本被截斷；ref 用分支名即可（與 GitHub API 一致）
$bodyObj = [ordered]@{ ref = $Branch; inputs = [ordered]@{ mode = $Mode } }
$json = $bodyObj | ConvertTo-Json -Compress -Depth 5

try {
    $resp = Invoke-WebRequest -UseBasicParsing -Uri $dispatchUri -Method Post -Headers $headers -Body $json `
        -ContentType "application/json; charset=utf-8"
    Write-Host "[ok] HTTP $($resp.StatusCode) workflow_dispatch sent (daily.yml mode=$Mode)"
}
catch {
    $code = $null
    $apiBody = $null
    $httpResp = $null
    if ($_.Exception.Response) { $httpResp = $_.Exception.Response }
    elseif ($_.Exception -is [System.Net.WebException] -and $_.Exception.Response) {
        $httpResp = $_.Exception.Response
    }
    if ($httpResp) {
        try {
            $code = [int]$httpResp.StatusCode
            $rs = $httpResp.GetResponseStream()
            if ($rs) {
                $reader = New-Object System.IO.StreamReader($rs, [System.Text.Encoding]::UTF8)
                $apiBody = $reader.ReadToEnd()
            }
        }
        catch { }
    }
    if (-not $apiBody -and $_.ErrorDetails.Message) { $apiBody = $_.ErrorDetails.Message }
    # With $ErrorActionPreference = Stop, Write-Error terminates before later lines run -- print diagnostics first.
    Write-Host "[error] POST workflow_dispatch failed (HTTP $code): $($_.Exception.Message)"
    if ($apiBody) { Write-Host "[github response] $apiBody" }
    if ($code -eq 403) {
        Write-Host "[hint] Classic PAT: enable scope 'workflow' (required to dispatch workflows)."
        Write-Host "[hint] Fine-grained PAT: this repo -> Actions: Read and write."
        Write-Host "[hint] Organization repo: authorize token for SSO (token settings)."
    }
    exit 1
}

Start-Sleep -Seconds 4

$runsUri = "https://api.github.com/repos/$Owner/$Repo/actions/workflows/daily.yml/runs?per_page=5"
try {
    $data = Invoke-RestMethod -Uri $runsUri -Headers $headers -Method Get
    Write-Host ""
    Write-Host "--- Last 5 runs (daily workflow) ---"
    foreach ($r in $data.workflow_runs) {
        $ev = $r.event
        $st = $r.status
        $cn = if ($r.conclusion) { $r.conclusion } else { "-" }
        Write-Host ("  [{0}] {1} / {2} event={3} {4}" -f $r.created_at, $st, $cn, $ev, $r.html_url)
    }
    Write-Host ""
    Write-Host "If the top row is workflow_dispatch and queued/in_progress, trigger worked."
}
catch {
    Write-Warning "Could not list runs: $($_.Exception.Message)"
}

$logPath = Join-Path $scriptDir "trigger_dispatch.log"
try {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') OK branch=$Branch mode=$Mode" | Add-Content -LiteralPath $logPath -Encoding UTF8
    Write-Host "[info] Log: $logPath"
}
catch {
    Write-Warning "Could not write log: $($_.Exception.Message)"
}
