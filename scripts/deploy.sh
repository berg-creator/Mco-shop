#!/usr/bin/env bash
# Развернуть магазин на чистом Ubuntu 24.04.
#
# Всё, что делает скрипт, идемпотентно: повторный запуск обновляет код и
# перезапускает сервис, ничего не ломая. Поэтому он же служит выкладкой
# новой версии, а не только первой установкой.
#
#   scripts/deploy.sh            развернуть/обновить (IP из .env.deploy)
#   scripts/deploy.sh --code     только код и перезапуск, без системных пакетов
#   scripts/deploy.sh --cert     выпустить сертификат (когда DNS доехал)
#   scripts/deploy.sh --pull     забрать с сервера базу и фото на мак
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env.deploy ] || { echo "нет .env.deploy"; exit 1; }
# shellcheck disable=SC1091
source .env.deploy
: "${SERVER_IP:?не задан SERVER_IP в .env.deploy}"
: "${DOMAIN:?не задан DOMAIN в .env.deploy}"

SSH="ssh -o BatchMode=yes -o ConnectTimeout=15"
mode="${1:-}"

system() {
  echo "== система =="
  $SSH "root@$SERVER_IP" DOMAIN="$DOMAIN" 'bash -s' <<'REMOTE'
set -e
export DEBIAN_FRONTEND=noninteractive
timedatectl set-timezone Europe/Moscow

# 961 МБ памяти хватает в покое, но apt и certbot дают пики.
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

apt-get update -qq
apt-get install -y -qq python3-venv nginx certbot python3-certbot-nginx ufw unattended-upgrades rsync >/dev/null

id deploy >/dev/null 2>&1 || adduser --disabled-password --gecos "" deploy
usermod -aG sudo deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys
install -d -o deploy -g deploy /opt/shop

# 00- в имени важно: OpenSSH берёт первое найденное значение, а
# 50-cloud-init.conf включает вход по паролю. Наш файл читается раньше.
cat > /etc/ssh/sshd_config.d/00-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
EOF
sshd -t && systemctl reload ssh

ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
sed -i 's|^//Unattended-Upgrade::Automatic-Reboot ".*";|Unattended-Upgrade::Automatic-Reboot "false";|' \
  /etc/apt/apt.conf.d/50unattended-upgrades

cat > /etc/nginx/sites-available/shop <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 20m;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
ln -sf /etc/nginx/sites-available/shop /etc/nginx/sites-enabled/shop
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

cat > /etc/systemd/system/shop.service <<'EOF'
[Unit]
Description=Mco shop: бот и витрина
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=deploy
WorkingDirectory=/opt/shop
ExecStart=/opt/shop/.venv/bin/python -m src.app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
REMOTE
}

code() {
  echo "== код и данные =="
  rsync -az --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude '.env' --exclude '.env.deploy' --exclude 'data' \
    -e "$SSH" ./ "deploy@$SERVER_IP:/opt/shop/"
  # --ignore-existing, а не просто «без --delete»: на сервере база живая, в ней
  # заявки и остатки. Обычный rsync затирал её локальной копией — выкладка кода
  # съедала продажи. Теперь наверх уезжает только то, чего там ещё нет
  # (первый разворот и новые фото), а увезти данные домой умеет --pull.
  rsync -az --ignore-existing -e "$SSH" data/ "deploy@$SERVER_IP:/opt/shop/data/"

  sed "s|^WEBAPP_URL=.*|WEBAPP_URL=https://$DOMAIN/app/|" .env \
    | $SSH "deploy@$SERVER_IP" 'cat > /opt/shop/.env && chmod 600 /opt/shop/.env'

  $SSH "deploy@$SERVER_IP" 'bash -s' <<'REMOTE'
set -e
cd /opt/shop
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m src.app --check
REMOTE
  $SSH "root@$SERVER_IP" 'systemctl enable --now shop >/dev/null; systemctl restart shop'
}

cert() {
  echo "== сертификат =="
  $SSH "root@$SERVER_IP" \
    "certbot --nginx -d $DOMAIN --non-interactive --agree-tos --register-unsafely-without-email --redirect"
  $SSH "root@$SERVER_IP" 'certbot renew --dry-run 2>&1 | tail -3'
}

check() {
  echo "== проверка =="
  $SSH "root@$SERVER_IP" 'systemctl is-active shop; journalctl -u shop -n 8 --no-pager -o cat'
  # Магазин встаёт около четырёх секунд: сначала бот здоровается с Telegram,
  # и только потом витрина занимает порт. Проверка сразу после restart попадала
  # в этот промежуток и каждый раз пугала ложным 502.
  local status=""
  for _ in $(seq 15); do
    status=$(curl -s -m 15 -o /dev/null -w "%{http_code}" "https://$DOMAIN/app/" || true)
    [ "$status" = "200" ] && break
    sleep 2
  done
  echo "витрина снаружи: $status"
}

# Сервер копирует базу сам раз в сутки, но копия рядом с оригиналом — не копия.
# Эта команда забирает данные на мак, её нужно иногда запускать руками.
pull() {
  echo "== забираю данные с сервера =="
  mkdir -p data backups
  rsync -az -e "$SSH" "deploy@$SERVER_IP:/opt/shop/data/" data/
  rsync -az -e "$SSH" "deploy@$SERVER_IP:/opt/shop/backups/" backups/
  echo "база: $(ls -la data/shop.db | awk '{print $5}') байт, фото: $(ls data/photos | wc -l | tr -d ' ')"
}

case "$mode" in
  --code) code; check ;;
  --cert) cert; check ;;
  --pull) pull ;;
  "")     system; code; check ;;
  *)      echo "неизвестный ключ: $mode"; exit 1 ;;
esac
