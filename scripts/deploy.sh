#!/bin/bash
# master 更新時に Bot を安全に再デプロイするスクリプト
# - db/ と .env は Git 管理外のため上書きされない
# - db/ が存在しない場合は中断する
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-$HOME/services/discord-bots/timecard-discord-bot}"
cd "$DEPLOY_DIR"

if [[ ! -d db ]]; then
  echo "ERROR: db/ が見つかりません。データ保護のため中断します。" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "ERROR: .env が見つかりません。中断します。" >&2
  exit 1
fi

git fetch origin master

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/master)

if [[ "$LOCAL" == "$REMOTE" ]]; then
  echo "$(date -Iseconds) Already up to date ($LOCAL)"
  exit 0
fi

echo "$(date -Iseconds) Updating $LOCAL -> $REMOTE"
git pull origin master

docker compose build
docker compose up -d --remove-orphans

echo "$(date -Iseconds) Deployed $(git rev-parse --short HEAD)"
