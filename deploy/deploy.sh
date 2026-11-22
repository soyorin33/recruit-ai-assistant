#!/usr/bin/env bash
# 一键部署到云服务器（需已能 SSH 免密登录，服务器已装 Docker）。详细教程见 docs/deploy-guide.md
#
# 用法：deploy/deploy.sh <服务器IP> [SSH用户，默认 root] [访问方式]
#   访问方式：
#     http          直接用 http://<IP> 访问（国内地域、没有备案域名时用这个）
#     <你的域名>     用 https://<域名> 访问，Caddy 自动申请证书（域名需已解析到服务器；国内地域需已备案）
#     不填          用免费的 https://<IP>.sslip.io（只适合香港等境外地域）
#   环境变量：
#     SSH_KEY        SSH 私钥路径，默认 ~/.ssh/recruit_deploy
#     PIP_INDEX_URL  pip 镜像源；国内服务器建议 https://mirrors.cloud.tencent.com/pypi/simple
#
# 本机 .env 会复制到服务器；访问口令和 MySQL 密码只在服务器首次部署时随机生成，保存在 deploy/server.env。
set -euo pipefail
HOST=${1:?用法：deploy/deploy.sh <服务器IP> [用户] [http | 域名]}
USER_=${2:-root}
MODE=${3:-$(echo "$HOST" | tr . -).sslip.io}
KEY=${SSH_KEY:-$HOME/.ssh/recruit_deploy}
PIP_INDEX_URL=${PIP_INDEX_URL:-https://pypi.org/simple}
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new $USER_@$HOST"
DIR=/opt/recruit-ai
SUDO=$([ "$USER_" = root ] && echo "" || echo "sudo")
if [ "$MODE" = http ]; then
  DOMAIN=$HOST; CADDYFILE=Caddyfile.http; URL="http://$HOST"
else
  DOMAIN=$MODE; CADDYFILE=Caddyfile; URL="https://$MODE"
fi
cd "$(dirname "$0")/.."

echo "==> 同步代码到 $USER_@$HOST:$DIR"
$SSH "$SUDO mkdir -p $DIR && $SUDO chown \$(id -u):\$(id -g) $DIR"
rsync -az --delete -e "ssh -i $KEY" \
  --exclude .venv --exclude .git --exclude data --exclude .env --exclude 'deploy/server.env' \
  --exclude __pycache__ --exclude .pytest_cache --exclude .DS_Store ./ "$USER_@$HOST:$DIR/"
[ -f .env ] && scp -q -i "$KEY" .env "$USER_@$HOST:$DIR/.env" && $SSH "chmod 600 $DIR/.env"

echo "==> 构建并启动（$URL）"
$SSH "cd $DIR && mkdir -p data && touch deploy/server.env && chmod 600 deploy/server.env
  grep -q '^ACCESS_TOKEN=' deploy/server.env || printf 'ACCESS_TOKEN=%s\nSEED_DEMO_DATA=1\n' \"\$(openssl rand -hex 8)\" >> deploy/server.env
  grep -q '^MYSQL_PASSWORD=' deploy/server.env || printf 'MYSQL_PASSWORD=%s\n' \"\$(openssl rand -hex 16)\" >> deploy/server.env
  # 访问方式也记进 server.env，之后在服务器上手动执行 docker compose 时不用再设置环境变量
  { grep -vE '^(DOMAIN|CADDYFILE|PIP_INDEX_URL)=' deploy/server.env || true
    printf 'DOMAIN=%s\nCADDYFILE=%s\nPIP_INDEX_URL=%s\n' '$DOMAIN' '$CADDYFILE' '$PIP_INDEX_URL'; } > deploy/server.env.tmp
  mv deploy/server.env.tmp deploy/server.env && chmod 600 deploy/server.env
  $SUDO docker compose --env-file deploy/server.env -f deploy/docker-compose.prod.yml up -d --build"

echo "==> 完成：$URL"
[ "$CADDYFILE" = Caddyfile ] && echo "    首次申请 HTTPS 证书约需 30 秒"
echo "    查看访问口令：$SSH 'grep ACCESS_TOKEN $DIR/deploy/server.env'"
