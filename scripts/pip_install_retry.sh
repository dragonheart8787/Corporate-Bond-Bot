#!/usr/bin/env bash
# 安裝 requirements，失敗時指數退避重試；最終失敗一定以非 0 結束並留下 ::error::。
#
# 為什麼需要：2026-10-07 bot run 946 在這一步被 PyPI 一次短暫失敗
# （ResolutionImpossible ... numpy: no matching distributions）擊倒，整個 bot 沒起來，
# 而 pip install 當時沒有任何重試。
#
# 用法：bash scripts/pip_install_retry.sh [requirements.txt]
# 可調（環境變數）：PIP_MAX_ATTEMPTS（預設 5）、PIP_RETRY_BASE_DELAY 秒（預設 5，每次加倍）
set -uo pipefail   # 刻意不加 -e：失敗要由下面的迴圈處理，而不是直接中斷

REQ="${1:-requirements.txt}"
MAX_ATTEMPTS="${PIP_MAX_ATTEMPTS:-5}"
DELAY="${PIP_RETRY_BASE_DELAY:-5}"

attempt=1
while true; do
  echo "▶ pip install -r ${REQ}（第 ${attempt}/${MAX_ATTEMPTS} 次）"
  if python -m pip install --disable-pip-version-check --retries 3 --timeout 30 -r "${REQ}"; then
    echo "✅ 套件安裝完成（第 ${attempt} 次成功）"
    exit 0
  fi

  if [ "${attempt}" -ge "${MAX_ATTEMPTS}" ]; then
    echo "::error title=套件安裝最終失敗::pip install 連續 ${MAX_ATTEMPTS} 次失敗（PyPI 或網路問題），此 run 標記為失敗，不會假裝成功。"
    exit 1
  fi

  echo "::warning title=pip install 失敗::${DELAY} 秒後重試（第 ${attempt}/${MAX_ATTEMPTS} 次失敗）"
  sleep "${DELAY}"
  DELAY=$((DELAY * 2))
  attempt=$((attempt + 1))
done
