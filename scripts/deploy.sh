#!/bin/bash
# main 更新時に Bot を安全に再デプロイするスクリプト
# - db/ と .env は Git 管理外のため上書きされない
# - db/ が存在しない場合は中断する
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-$HOME/services/discord-bots/timecard-discord-bot}"
cd "$DEPLOY_DIR"
export DB_VOLUME="${DB_VOLUME:-${DEPLOY_DIR}/db}"
LOCK_FILE="${TIMECARD_DEPLOY_LOCK_FILE:-${DEPLOY_DIR}/deploy/.deploy.lock}"

mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Another deployment or maintenance operation is already running."
  exit 75
fi

restore_repository_ownership() {
  if [[ "$(id -u)" -ne 0 ]]; then
    return
  fi

  local owner_uid="${DEPLOY_OWNER_UID:-$(stat -c '%u' "$DEPLOY_DIR")}"
  local owner_gid="${DEPLOY_OWNER_GID:-$(stat -c '%g' "$DEPLOY_DIR")}"
  local path parent

  chown -R "${owner_uid}:${owner_gid}" .git
  chown "${owner_uid}:${owner_gid}" .

  while IFS= read -r -d '' path; do
    chown -h "${owner_uid}:${owner_gid}" "$path"
    parent=$(dirname "$path")
    while [[ "$parent" != "." ]]; do
      chown "${owner_uid}:${owner_gid}" "$parent"
      parent=$(dirname "$parent")
    done
  done < <(git ls-files -z)
}

# Webhookコンテナはrootで実行されるため、デプロイ後にホスト側の所有権を戻す。
trap restore_repository_ownership EXIT

if [[ ! -d db ]]; then
  echo "ERROR: db/ が見つかりません。データ保護のため中断します。" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "ERROR: .env が見つかりません。中断します。" >&2
  exit 1
fi

git fetch origin main

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [[ "$LOCAL" == "$REMOTE" ]]; then
  echo "$(date -Iseconds) Already up to date ($LOCAL)"
  exit 0
fi

echo "$(date -Iseconds) Updating $LOCAL -> $REMOTE"
git merge --ff-only origin/main

docker compose build
docker compose up -d --force-recreate --remove-orphans

echo "$(date -Iseconds) Deployed $(git rev-parse --short HEAD)"
