#!/usr/bin/env bash
# 一键部署到云服务器（需已能 SSH 免密登录，服务器已装 Docker）。
# 用法：deploy/deploy.sh <服务器IP> [SSH用户，默认 root] [域名，默认 <IP>.sslip.io]
# 本机 .env 会复制到服务器；访问口令和 MySQL 密码只在服务器首次部署时随机生成，保存在 deploy/server.env。
set -euo pipefail
HOST=${1:?用法：deploy/deploy.sh <服务器IP> [用户] [域名]}
USER_=${2:-root}
DOMAIN=${3:-$(echo "$HOST" | tr . -).sslip.io}
KEY=${SSH_KEY:-$HOME/.ssh/recruit_deploy}
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new $USER_@$HOST"
DIR=/opt/recruit-ai
SUDO=$([ "$USER_" = root ] && echo "" || echo "sudo")
cd "$(dirname "$0")/.."

echo "==> 同步代码到 $USER_@$HOST:$DIR"
$SSH "$SUDO mkdir -p $DIR && $SUDO chown \$(id -u):\$(id -g) $DIR"
rsync -az --delete -e "ssh -i $KEY" \
  --exclude .venv --exclude .git --exclude data --exclude .env --exclude 'deploy/server.env' \
  --exclude __pycache__ --exclude .pytest_cache --exclude .DS_Store ./ "$USER_@$HOST:$DIR/"
[ -f .env ] && scp -q -i "$KEY" .env "$USER_@$HOST:$DIR/.env"

echo "==> 构建并启动（域名 $DOMAIN）"
$SSH "cd $DIR && touch deploy/server.env && chmod 600 deploy/server.env
  grep -q '^ACCESS_TOKEN=' deploy/server.env || printf 'ACCESS_TOKEN=%s\nSEED_DEMO_DATA=1\n' \"\$(openssl rand -hex 4)\" >> deploy/server.env
  grep -q '^MYSQL_PASSWORD=' deploy/server.env || printf 'MYSQL_PASSWORD=%s\n' \"\$(openssl rand -hex 16)\" >> deploy/server.env
  $SUDO env DOMAIN=$DOMAIN docker compose --env-file deploy/server.env -f deploy/docker-compose.prod.yml up -d --build"

echo "==> 完成：https://$DOMAIN （首次申请证书约需 30 秒）"
echo "    访问口令：ssh 到服务器查看 $DIR/deploy/server.env"
