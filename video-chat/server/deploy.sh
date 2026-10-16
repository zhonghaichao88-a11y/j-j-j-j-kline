#!/usr/bin/env bash
# 一键部署视频房间（Ubuntu 22.04 / 24.04，用 root 运行）
#   用法：bash deploy.sh 你的域名
#   例如：bash deploy.sh v.example.com
# 会安装：Node.js、Caddy（自动申请 https 证书）、coturn（TURN 中转），并设置开机自启。
set -euo pipefail

DOMAIN="${1:-}"
if [[ -z "$DOMAIN" ]]; then
  echo "用法：bash deploy.sh 你的域名（域名要先解析到这台服务器的公网 IP）" >&2
  exit 1
fi
if [[ $EUID -ne 0 ]]; then
  echo "请用 root 运行（sudo bash deploy.sh $DOMAIN）" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"   # video-chat 文件夹
INSTALL_DIR=/opt/video-chat
APP_DIR=$INSTALL_DIR/server
ENV_FILE=/etc/video-chat.env

echo "==> 安装软件"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y curl ca-certificates openssl coturn gnupg
if ! apt-get install -y caddy; then
  # Ubuntu 22.04 的软件源里没有 caddy，用官方源
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi
if ! command -v node >/dev/null || [[ "$(node -p 'process.versions.node.split(".")[0]')" -lt 18 ]]; then
  # Ubuntu 24.04 自带 Node 18；22.04 自带的太旧，用 NodeSource 装 20
  if apt-cache policy nodejs | grep -qE 'Candidate: (1[89]|2[0-9])'; then
    apt-get install -y nodejs npm
  else
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
  fi
fi

echo "==> 复制程序到 $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/server"
cp "$SRC_DIR/index.html" "$SRC_DIR/peerjs.min.js" "$INSTALL_DIR/"
cp "$SRC_DIR/server/server.js" "$SRC_DIR/server/package.json" "$INSTALL_DIR/server/"
[[ -f "$SRC_DIR/server/package-lock.json" ]] && cp "$SRC_DIR/server/package-lock.json" "$INSTALL_DIR/server/"
chmod -R a+rX "$INSTALL_DIR"

echo "==> 安装依赖"
cd "$APP_DIR"
# 国内服务器用 npmmirror 镜像，海外/香港直接用官方源
if ! curl -s -m 5 -o /dev/null https://registry.npmjs.org/; then
  NPM_REG=(--registry=https://registry.npmmirror.com)
else
  NPM_REG=()
fi
npm install --omit=dev --no-audit --no-fund "${NPM_REG[@]}"

echo "==> 生成 TURN 密钥"
if [[ -f "$ENV_FILE" ]] && grep -q '^TURN_SECRET=' "$ENV_FILE"; then
  TURN_SECRET="$(grep '^TURN_SECRET=' "$ENV_FILE" | cut -d= -f2-)"
else
  TURN_SECRET="$(openssl rand -hex 24)"
fi
cat > "$ENV_FILE" <<EOF
PORT=9000
TURN_HOST=$DOMAIN
TURN_SECRET=$TURN_SECRET
EOF
chmod 600 "$ENV_FILE"

echo "==> 配置 coturn"
PUBLIC_IP="$(curl -s -4 -m 5 https://api.ipify.org || curl -s -4 -m 5 https://myip.ipip.net/s || true)"
PRIVATE_IP="$(hostname -I | awk '{print $1}')"
if [[ -n "$PUBLIC_IP" && "$PUBLIC_IP" != "$PRIVATE_IP" ]]; then
  EXTERNAL="external-ip=$PUBLIC_IP/$PRIVATE_IP"   # 云服务器一般是内网 IP + 公网映射
elif [[ -n "$PUBLIC_IP" ]]; then
  EXTERNAL="external-ip=$PUBLIC_IP"
else
  EXTERNAL="# 没查到公网 IP，如果中转不工作，在这里填 external-ip=公网IP/内网IP"
fi
cat > /etc/turnserver.conf <<EOF
listening-port=3478
fingerprint
use-auth-secret
static-auth-secret=$TURN_SECRET
realm=$DOMAIN
$EXTERNAL
min-port=49160
max-port=49200
total-quota=100
stale-nonce=600
no-multicast-peers
no-cli
no-tls
no-dtls
# 禁止通过中转访问服务器内网
denied-peer-ip=10.0.0.0-10.255.255.255
denied-peer-ip=172.16.0.0-172.31.255.255
denied-peer-ip=192.168.0.0-192.168.255.255
denied-peer-ip=127.0.0.0-127.255.255.255
denied-peer-ip=169.254.0.0-169.254.255.255
denied-peer-ip=100.64.0.0-100.127.255.255
EOF
[[ -f /etc/default/coturn ]] && sed -i 's/^#\?TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn

echo "==> 配置网页服务"
cat > /etc/systemd/system/video-chat.service <<EOF
[Unit]
Description=Video chat rooms
After=network.target

[Service]
EnvironmentFile=$ENV_FILE
WorkingDirectory=$APP_DIR
ExecStart=$(command -v node) $APP_DIR/server.js
Restart=always
RestartSec=3
DynamicUser=yes
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:9000
}
EOF

echo "==> 打开防火墙端口"
if command -v ufw >/dev/null && ufw status | grep -q 'Status: active'; then
  ufw allow 80/tcp; ufw allow 443/tcp; ufw allow 3478; ufw allow 49160:49200/udp
fi

systemctl daemon-reload
systemctl enable --now video-chat coturn
systemctl restart video-chat coturn caddy

sleep 2
if curl -s -m 5 http://127.0.0.1:9000/config.json | grep -q selfHosted; then
  echo
  echo "部署完成：https://$DOMAIN"
  echo "还需要在云服务器控制台的「安全组/防火墙」里放行："
  echo "  TCP 80、443（网页）    TCP+UDP 3478（中转）    UDP 49160-49200（中转媒体）"
  echo "第一次打开 https 可能要等几十秒，Caddy 在自动申请证书。"
else
  echo "网页服务没有起来，看日志：journalctl -u video-chat -n 50" >&2
  exit 1
fi
