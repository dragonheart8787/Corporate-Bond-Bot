#Requires -Version 5.1
<#
.SYNOPSIS
  用 GitHub API 觸發「每日公司債」workflow（workflow_dispatch），不依賴 GitHub 內建 schedule。

.DESCRIPTION
  當 Actions 裡永遠沒有 Scheduled run 時，可用本機「工作排程器」每 15 分鐘執行此腳本代替 cron。

  1) GitHub → Settings → Developer settings → 建立 Fine-grained PAT
     僅授權 Corporate-Bond-Bot，勾選「Actions: Read and write」
     （或 Classic PAT：repo 範圍）
  2) 在 PowerShell 先設定： $env:GITHUB_PAT = "ghp_...."
  3) 執行： .\scripts\Trigger-GitHubDailyWorkflow.ps1

  工作排程器：建立基本工作 → 觸發程序每 15 分鐘 → 動作「啟動程式」
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\完整路徑\scripts\Trigger-GitHubDailyWorkflow.ps1"

.PARAMETER Mode
  對應 Actions 手動執行選項：all | send | fetch
#>
param(
    [string] $Pat = $env:GITHUB_PAT,
    [string] $Owner = "dragonheart8787",
    [string] $Repo = "Corporate-Bond-Bot",
    [string] $Branch = "master",
    [ValidateSet("all", "send", "fetch")]
    [string] $Mode = "all"
)

if (-not $Pat) {
    Write-Error "請設定環境變數 GITHUB_PAT（或傳入 -Pat）"
    exit 1
}

$uri = "https://api.github.com/repos/$Owner/$Repo/actions/workflows/daily.yml/dispatches"
$headers = @{
    Accept               = "application/vnd.github+json"
    Authorization        = "Bearer $Pat"
    "X-GitHub-Api-Version" = "2022-11-28"
}
$bodyObj = @{ ref = $Branch; inputs = @{ mode = $Mode } }
$body = $bodyObj | ConvertTo-Json -Compress

try {
    Invoke-RestMethod -Uri $uri -Method Post -Headers $headers -Body $body -ContentType "application/json; charset=utf-8"
    Write-Host "OK: 已觸發 daily.yml workflow_dispatch (mode=$Mode)。請到 GitHub Actions 查看 run。"
}
catch {
    Write-Error $_.Exception.Message
    if ($_.ErrorDetails.Message) { Write-Host $_.ErrorDetails.Message }
    exit 1
}
