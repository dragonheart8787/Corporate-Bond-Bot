#!/usr/bin/env bash
# 把已 commit 的內容推回分支：每次嘗試前先 pull --rebase，失敗就退避重試。
# 最終失敗：輸出 ::error::、送 Telegram 告警，並以非 0 結束（讓 run 變紅，不是只留 warning）。
#
# 為什麼要這樣：備援排程與 bot 觸發的日報可能接連跑，.last_sent 防重發標記一旦推不上去，
# 下一班就會重發日報。所以「Telegram 已送出但標記沒入庫」必須是會被看見的失敗。
#
# 環境變數：
#   PUSH_BRANCH        要推的分支（預設 GITHUB_REF_NAME，再退回 master）
#   PUSH_ATTEMPTS      最多嘗試次數（預設 4）
#   PUSH_RETRY_SLEEP   第 n 次失敗後等 n * 這個秒數（預設 3；測試設 0）
#   PUSH_ALERT_CMD     告警指令（預設 python3 bot_relay.py alert；接收訊息作為最後一個參數）
set -u

BRANCH="${PUSH_BRANCH:-${GITHUB_REF_NAME:-master}}"
ATTEMPTS="${PUSH_ATTEMPTS:-4}"
SLEEP_UNIT="${PUSH_RETRY_SLEEP:-3}"
ALERT_CMD="${PUSH_ALERT_CMD:-python3 bot_relay.py alert}"

for i in $(seq 1 "$ATTEMPTS"); do
  # -X theirs：rebase 時「theirs」是我們這個 commit。衝突只會發生在 reports/ 下我們剛產生的檔案
  # （最新報告與 .last_sent），以我們這次的內容為準才是對的。
  if git pull --rebase -X theirs origin "$BRANCH"; then
    if git push origin "HEAD:${BRANCH}"; then
      echo "✅ push 成功（第 ${i}/${ATTEMPTS} 次）"
      exit 0
    fi
    echo "push 失敗（${i}/${ATTEMPTS}）"
  else
    echo "pull --rebase 失敗（${i}/${ATTEMPTS}），中止 rebase 後重試"
    git rebase --abort 2>/dev/null || true
  fi
  if [ "$i" -lt "$ATTEMPTS" ]; then
    sleep $((i * SLEEP_UNIT))
  fi
done

MSG="每日報告已處理，但 reports/（含 .last_sent 防重發標記）推送失敗 ${ATTEMPTS} 次，下一班可能重發。run: ${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
echo "::error title=git push 最終失敗::${MSG}"
# 告警失敗只留 log，不能蓋掉原本的失敗結果
# shellcheck disable=SC2086
$ALERT_CMD "$MSG" || echo "⚠️ 告警也沒送出去（未設定或失敗），請直接查看 Actions 頁面"
exit 1
