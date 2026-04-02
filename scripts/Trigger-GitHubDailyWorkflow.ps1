#Requires -Version 5.1
<#
.SYNOPSIS
  用 GitHub API 觸發「每日公司債」workflow（workflow_dispatch）。

.DESCRIPTION
  GitHub 內建 schedule 若不觸發，請用「工作排程器」定期執行本腳本。

  權杖來源（擇一）：
  1) 環境變數 GITHUB_PAT
  2) 與本腳本同目錄的檔案 .github_pat_local（一行 token，勿提交 git；已列 .gitignore）

  工作排程器常見問題：排程工作**沒有**你的使用者環境變數 → 請用 .github_pat_local，
  或在工作內「動作」用：cmd /c "set GITHUB_PAT=xxx&& powershell.exe -File ..."（較不安全）。

  成功時 GitHub 回 204（無內容），本腳本會再查 API 印出最新 run 連結，避免「沒報錯以為沒跑」。

.PARAMETER Branch
  預設 auto = 向 GitHub 查詢 repo 的 default_branch（避免 master/main 搞錯）。
#>
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
$tokenFile = Join-Path $scriptDir ".github_pat_local"

if (-not $Pat) { $Pat = $env:GITHUB_PAT }
if (-not $Pat -and (Test-Path $tokenFile)) {
    $Pat = (Get-Content -LiteralPath $tokenFile -Raw -Encoding UTF8).Trim()
    Write-Host "ℹ️  已從 .github_pat_local 讀取 token"
}
if (-not $Pat) {
    Write-Error @"
請提供 GitHub PAT：
  - 設定環境變數 `$env:GITHUB_PAT，或
  - 在 scripts 目錄建立 .github_pat_local（一行 token，勿 push）
"@
    exit 1
}

$headers = @{
    Accept                 = "application/vnd.github+json"
    Authorization          = "Bearer $Pat"
    "X-GitHub-Api-Version" = "2022-11-28"
}

if ($Branch -eq "auto") {
    $repoUri = "https://api.github.com/repos/$Owner/$Repo"
    $info = Invoke-RestMethod -Uri $repoUri -Headers $headers -Method Get
    $Branch = $info.default_branch
    Write-Host "ℹ️  使用 repo 預設分支: $Branch"
}

$dispatchUri = "https://api.github.com/repos/$Owner/$Repo/actions/workflows/daily.yml/dispatches"
$bodyObj = @{ ref = $Branch; inputs = @{ mode = $Mode } }
$json = $bodyObj | ConvertTo-Json -Compress

try {
    $resp = Invoke-WebRequest -UseBasicParsing -Uri $dispatchUri -Method Post -Headers $headers -Body $json `
        -ContentType "application/json; charset=utf-8"
    Write-Host "✅ HTTP $($resp.StatusCode) — 已送出 workflow_dispatch（daily.yml, mode=$Mode）"
}
catch {
    $code = $null
    if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
    Write-Error "POST 失敗 (HTTP $code): $($_.Exception.Message)"
    if ($_.ErrorDetails.Message) { Write-Host $_.ErrorDetails.Message }
    exit 1
}

Start-Sleep -Seconds 4

$runsUri = "https://api.github.com/repos/$Owner/$Repo/actions/workflows/daily.yml/runs?per_page=5"
try {
    $data = Invoke-RestMethod -Uri $runsUri -Headers $headers -Method Get
    Write-Host ""
    Write-Host "—— 最近 5 筆「每日公司債」runs ——"
    foreach ($r in $data.workflow_runs) {
        $ev = $r.event
        $st = $r.status
        $cn = if ($r.conclusion) { $r.conclusion } else { "-" }
        Write-Host ("  [{0}] {1} / {2}  event={3}  {4}" -f $r.created_at, $st, $cn, $ev, $r.html_url)
    }
    Write-Host ""
    Write-Host "若最上面一筆是幾秒內的 workflow_dispatch 且 in_progress/queued，代表觸發成功。"
}
catch {
    Write-Warning "無法取得 runs 列表: $($_.Exception.Message)"
}

$logPath = Join-Path $scriptDir "trigger_dispatch.log"
try {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') OK branch=$Branch mode=$Mode" | Add-Content -LiteralPath $logPath -Encoding UTF8
    Write-Host "ℹ️  已寫入 log: $logPath（工作排程器是否有跑，可看此檔時間戳）"
}
catch { }
