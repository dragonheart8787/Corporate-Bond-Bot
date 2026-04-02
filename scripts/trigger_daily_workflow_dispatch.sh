#!/usr/bin/env bash
# 觸發 GitHub Actions「每日公司債」workflow_dispatch（不依賴內建 schedule）
# 用法：export GITHUB_PAT=ghp_xxx  && ./scripts/trigger_daily_workflow_dispatch.sh
set -euo pipefail
OWNER="${OWNER:-dragonheart8787}"
REPO="${REPO:-Corporate-Bond-Bot}"
BRANCH="${BRANCH:-master}"
MODE="${1:-all}"

if [[ -z "${GITHUB_PAT:-}" ]]; then
  echo "請設定環境變數 GITHUB_PAT" >&2
  exit 1
fi

curl -sS -L -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer ${GITHUB_PAT}" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/daily.yml/dispatches" \
  -d "{\"ref\":\"${BRANCH}\",\"inputs\":{\"mode\":\"${MODE}\"}}"

echo ""
echo "OK: 已請求觸發 daily.yml (mode=${MODE})"
