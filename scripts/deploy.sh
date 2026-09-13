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

# Через VPN с маленьким MTU наши пакеты не пролезают, а ICMP «слишком большой»
# до сервера не доходит: страница уезжает в никуда, покупатель видит пустой
# экран. tcp_mtu_probing учит ядро самому уменьшать сегменты, когда ответ
# перестал доходить, — вместо того чтобы вечно повторять тот же большой пакет.
cat > /etc/sysctl.d/99-shop.conf <<'EOF'
net.ipv4.tcp_mtu_probing = 1
EOF
sysctl -q --system

apt-get update -qq
# ffmpeg — ради одной команды: он декодирует присланную владельцем песню,
# чтобы src/music.py померил её темп. Без него магазин работает, просто
# фото листаются раз в три секунды вместо ритма.
apt-get install -y -qq python3-venv nginx certbot python3-certbot-nginx ufw unattended-upgrades rsync ffmpeg sqlite3 >/dev/null

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
# Заплатки приезжают сами, но пока сервер не перезагрузили, заплатка на ядро
# не работает — а ручная перезагрузка «когда-нибудь» не случается никогда.
# Пять утра: покупателей нет, магазин поднимется сам (enable + Restart=always).
# Файл 52-й: apt читает по возрастанию, и наше значение перебивает 50-е.
cat > /etc/apt/apt.conf.d/52shop-reboot <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "05:00";
EOF

# Журнал за две недели съел 124 МБ на диске в 4,9 ГБ — при том, что читают
# из него последние пару дней и только когда что-то сломалось.
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/shop.conf <<'EOF'
[Journal]
SystemMaxUse=200M
EOF
systemctl restart systemd-journald

# Свой формат лога: в обычном не видно, сколько длился запрос и чем клиент
# назвался. А когда витрина «не открывается» у одного покупателя из десяти,
# разбираться приходится именно по этим полям — своего экрана у нас нет.
cat > /etc/nginx/conf.d/shop-log.conf <<'EOF'
log_format shop '$remote_addr $time_local "$request" $status '
                'отдано=$body_bytes_sent за=$request_time '
                'сжатие="$http_accept_encoding" tls=$ssl_protocol "$http_user_agent"';
EOF

cat > /etc/nginx/sites-available/shop <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 20m;
    access_log /var/log/nginx/shop.log shop;
    # nginx по умолчанию жмёт только text/html, а витрина — это css и js
    # на полсотни килобайт: без этой строки они едут целиком. Шрифт, mp3
    # и фотографии сжаты уже сами, второй раз их жать незачем.
    gzip_types text/css text/javascript application/javascript application/json image/svg+xml;
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

# Секцию :443 в этот же файл дописывает certbot, а конфиг выше пишется заново
# на каждый полный прогон — и https пропадал вместе с ним. Витрина при этом
# отвечала по http, systemctl показывал active, а мини-приложение Telegram,
# которому нужен только https, было мертво. Если сертификат уже выпущен,
# возвращаем секцию тем же certbot: он ничего не перевыпускает, только правит
# конфиг, и запускать выкладку целиком снова становится безопасно.
if [ -d "/etc/letsencrypt/live/$DOMAIN" ]; then
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
    --register-unsafely-without-email --redirect --keep-until-expiring
  nginx -t && systemctl reload nginx
fi

# Присмотр за магазином. Всё, что ниже, отвечает на один вопрос: кто заметит,
# что сломалось, если никто не смотрит. Витрина умеет умереть тихо — systemctl
# показывает active, а мини-приложение уже мертво, — и копия базы четыре ночи
# подряд падала так же тихо. Сообщение приходит владельцу тем же ботом.

cat > /usr/local/bin/shop-alert <<'EOF'
#!/bin/bash
# Сообщение владельцу ботом магазина: токен и id уже лежат в .env, заводить
# ради тревог второй канал незачем.
#
#   shop-alert "текст"
#   shop-alert --file копия.db "подпись"
#
# .env читается построчно, а не через source: в DELIVERY_OPTIONS значения
# разделены точкой с запятой, а для bash точка с запятой — конец команды.
set -u
val() { sed -n "s/^$1=//p" /opt/shop/.env | tail -1; }
api="https://api.telegram.org/bot$(val TELEGRAM_BOT_TOKEN)"
file=""
if [ "${1:-}" = "--file" ]; then file="$2"; shift 2; fi
for id in $(val ADMIN_IDS | tr ',' ' '); do
  if [ -n "$file" ]; then
    curl -sS -m 120 -o /dev/null -F "chat_id=$id" -F "document=@$file" \
      -F "caption=$*" "$api/sendDocument" || true
  else
    curl -sS -m 20 -o /dev/null --data-urlencode "chat_id=$id" \
      --data-urlencode "text=$*" "$api/sendMessage" || true
  fi
done
exit 0
EOF
chmod 755 /usr/local/bin/shop-alert

cat > /usr/local/bin/shop-backup <<'EOF'
#!/bin/bash
# Копия базы: каждую ночь рядом, по понедельникам — владельцу в Telegram.
# Копия на том же диске не переживёт смерти диска, а второй сервер ради
# ста двадцати килобайт заводить незачем: Telegram хранит файл бесплатно.
set -e
d=/opt/shop/backups
# mkdir, а не «папка же есть»: выкладка кода однажды снесла её вместе с копиями,
# и служба падала четыре ночи подряд, пока никто не смотрел.
mkdir -p "$d"
f="$d/shop-$(date +%F).db"
sqlite3 /opt/shop/data/shop.db ".backup '$f'"
# Непроверенная копия — не копия: битый файл того же размера выглядит как
# удачная ночь ровно до дня, когда он понадобится.
sqlite3 "$f" 'PRAGMA integrity_check' | grep -qx ok
# Держим две недели: диск 4,9 ГБ, база 128 КБ, глубже незачем.
find "$d" -name 'shop-*.db' -mtime +14 -delete
if [ "$(date +%u)" = 1 ]; then
  shop-alert --file "$f" "копия базы магазина за $(date +%F)"
fi
EOF
chmod 755 /usr/local/bin/shop-backup

cat > /usr/local/bin/shop-watch <<'EOF'
#!/bin/bash
# Сторож: смотрит на то, что ломается тихо, и сначала пробует вылечить сам.
#
# Лечится только то, что уже случалось и что безопасно сделать вслепую:
# перезапустить, убрать свой же мусор, повторить то, что и так делается
# по расписанию. Незнакомое состояние не лечится — сторож, который чинит
# непонятое, из мелкой поломки делает потерю данных: «мало места» → стёр
# самое крупное → каталог без фотографий.
#
# Что вылечилось — тоже сообщается. Починка, которая молча случается каждый
# час, прячет настоящую поломку: магазин вроде работает, а лежит он трижды
# в сутки, и никто об этом не знает.
set -u
state=/var/lib/shop-watch.state
stamp=/var/lib/shop-watch.restarted
healed=""
bad=""

# — копия базы —
# Её делает ночной таймер. Если свежей нет, сторож просто выполняет ту же
# команду сейчас: это не догадка, а повтор того, что и так должно было быть.
fresh() { [ -n "$(find /opt/shop/backups -name 'shop-*.db' -mtime -2 2>/dev/null)" ]; }
if ! fresh; then
  systemctl start shop-backup >/dev/null 2>&1
  if fresh; then
    healed="$healed- свежей копии не было, сделал сейчас"$'\n'
  else
    bad="$bad- копию базы сделать не удалось"$'\n'
  fi
fi

# — место на диске —
# Чистим только своё и одноразовое: журнал и кеш пакетов. Музыку, фото
# и старые копии не трогаем — что из них лишнее, решает владелец, а не
# сторож в три часа ночи.
space() { df -Pm /opt/shop | awk 'NR==2 {print $4}'; }
if [ "$(space)" -lt 500 ]; then
  journalctl --vacuum-size=100M >/dev/null 2>&1
  apt-get clean >/dev/null 2>&1
  healed="$healed- было мало места, почистил журнал и кеш пакетов: стало $(space) МБ"$'\n'
  [ "$(space)" -lt 500 ] && bad="$bad- на диске всё равно $(space) МБ, дальше чистить нечего"$'\n'
fi

# — витрина снаружи —
# Единственная честная проверка: по https и с той стороны. systemctl про
# мёртвое мини-приложение ничего не знает — процесс жив, порт занят, а
# Telegram в витрину уже не пускает.
url=$(sed -n 's/^WEBAPP_URL=//p' /opt/shop/.env | tail -1)
outside() { curl -s -m 20 -o /dev/null -w '%{http_code}' "$url" || echo "нет сети"; }
code=$(outside)
if [ "$code" != 200 ]; then
  # Перезапуск лечит зависший процесс, но не лечит сломанный код. Не чаще
  # раза в час: вторая попытка не поможет, зато скроет, что магазин лежит.
  if [ -z "$(find $stamp -mmin -60 2>/dev/null)" ]; then
    touch $stamp
    systemctl restart shop
    sleep 15
    code=$(outside)
  fi
  if [ "$code" = 200 ]; then
    healed="$healed- витрина не отвечала, перезапустил магазин — снова 200"$'\n'
  else
    # Хвост журнала прямо в сообщении: с него всё равно начнётся разбор,
    # а так он начнётся без ssh и с телефона.
    bad="$bad- витрина снаружи отвечает $code
$(journalctl -u shop -n 5 --no-pager -o cat)"$'\n'
  fi
fi

# — витрина целиком —
# «Страница отдалась» — ещё не работающий магазин. Без app.js Telegram
# показывает серый квадрат, а сторож бодро отвечает 200: проверка смотрела
# на index.html, то есть на единственный файл, который ломается последним.
# Тянем со страницы её же ссылки и дёргаем каждую: выкладка, потерявшая файл,
# видна сразу, а не когда покупатель постоит перед пустым экраном и уйдёт.
if [ "$code" = 200 ]; then
  gone=""
  # Файл может отдаваться и всё равно не доехать: nginx жмёт сжатое кусками,
  # без длины, и оборванная на мобильном связь оставляет в WebKit обрубок,
  # неотличимый от целого. Дальше сходится ETag, приходит 304 — и витрина
  # мертва навсегда, при живом сервере и зелёном 200. Дважды так и было:
  # сначала со страницей, потом с app.js. Лечится одним `no-store`, поэтому
  # сторож смотрит, что заголовок на месте: вернётся кэш — вернётся и беда.
  cached=""
  for f in $(curl -s -m 20 "$url" | grep -oE '(src|href)="[^"?]+' | cut -d'"' -f2); do
    [ "$(curl -s -m 20 -o /dev/null -w '%{http_code}' "$url$f")" = 200 ] || gone="$gone $f"
    curl -s -m 20 -I "$url$f" | grep -qi '^cache-control:.*no-store' || cached="$cached $f"
  done
  [ -n "$gone" ] && bad="$bad- витрина отвечает, но не отдаёт:$gone"$'\n'
  [ -n "$cached" ] && bad="$bad- витрина разрешает себя запомнить, обрывок осядет у покупателя навсегда:$cached"$'\n'
fi

# — музыка витрины —
# Тишина в витрине слышна только ушами: страница цела, файлы отдаются, а песни
# нет. Ломается это не выкладкой — данные она не трогает, — а переносом и
# восстановлением из копии: метка версии в адресе песни это mtime файла, и
# после переезда прежние адреса ведут в никуда. Дёргаем первую песню каталога:
# протух весь список разом, одной хватит.
if [ "$code" = 200 ]; then
  root="${url%/app/}"
  disk=$(grep -o '"file"' /opt/shop/data/music/tracks.json 2>/dev/null | wc -l)
  song=$(curl -s -m 20 "$root/api/catalog" | grep -o '/music/[^"]*' | head -1)
  if [ -z "$song" ]; then
    [ "$disk" -gt 0 ] && bad="$bad- в витрине тихо: песен на диске $disk, а каталог отдаёт пустой плейлист"$'\n'
  elif [ "$(curl -s -m 20 -I -o /dev/null -w '%{http_code}' "$root$song")" != 200 ]; then
    bad="$bad- песня из плейлиста не отдаётся: $song"$'\n'
  fi
fi

# — открытия доходят до конца —
# У владельца в кэше телефона осела половина страницы: css телефон тянул,
# а app.js не запрашивал ни разу — витрина не открывалась месяцами, и всё это
# время «снаружи 200» показывало порядок. Такое видно только со стороны
# клиента, и единственный её след — журнал nginx: адрес, который взял стили
# и не взял app.js. Одиночный случай — оборванная связь, его пропускаем;
# повтор с того же адреса — застрявший клиент, и это уже новость.
stuck=$(LC_ALL=C awk -v d="$(LC_ALL=C date +%d/%b/%Y)" '
  index($0, d) == 0 { next }
  /GET \/app\/styles\.css/ { css[$1]++ }
  /GET \/app\/app\.js/ { js[$1]++ }
  END { for (ip in css) if (!(ip in js) && css[ip] > 1) print ip }
' /var/log/nginx/shop.log 2>/dev/null | wc -l | tr -d ' ')
[ "${stuck:-0}" -gt 0 ] && bad="$bad- витрина открывается наполовину (адресов: $stuck): взяли стили, но не app.js"$'\n'

# — сертификат —
# Продлевает certbot.timer, дважды в сутки и за месяц до конца. Если срок
# всё-таки подошёл на десять дней, продление сломано: пробуем руками, и если
# не вышло — будим человека. Без https мини-приложение мертво целиком.
cert=$(ls /etc/letsencrypt/live/*/cert.pem 2>/dev/null | head -1)
if [ -n "$cert" ] && ! openssl x509 -checkend 864000 -noout -in "$cert" >/dev/null 2>&1; then
  certbot renew --quiet >/dev/null 2>&1
  if openssl x509 -checkend 864000 -noout -in "$cert" >/dev/null 2>&1; then
    healed="$healed- сертификат заканчивался, продлил"$'\n'
  else
    bad="$bad- сертификат кончается через считанные дни, продлить не вышло"$'\n'
  fi
fi

# Вылеченное шлём сразу: оно редкое, а если стало частым — это и есть новость.
[ -n "$healed" ] && shop-alert "магазин, само починилось:
$healed"

# На чистом сервере файла состояния нет, и «порядок» по умолчанию нужен,
# чтобы разворот не начинался с сообщения «всё снова в порядке» о том,
# что и не ломалось.
# `$(cat)` срезает хвостовой перевод строки, а в переменной он остаётся:
# сравнение не совпадало никогда, и про одну и ту же поломку сторож писал
# каждые четверть часа. Срезаем сразу, чтобы сравнивать одинаковое.
now=$(printf '%s' "${bad:-порядок}")
if [ "$now" != "$(cat $state 2>/dev/null || echo порядок)" ]; then
  echo "$now" > "$state"
  if [ -n "$bad" ]; then
    shop-alert "магазин, неладно:
$bad"
  else
    shop-alert "магазин: всё снова в порядке"
  fi
fi
exit 0
EOF
chmod 755 /usr/local/bin/shop-watch

# Шаблон: любая служба может попросить сказать о своём падении через
# OnFailure=shop-alert@%n.service, не зная ничего про Telegram.
cat > /etc/systemd/system/shop-alert@.service <<'EOF'
[Unit]
Description=Сказать владельцу, что %i не отработал

[Service]
Type=oneshot
ExecStart=/usr/local/bin/shop-alert "%i: служба не отработала, смотреть journalctl -u %i"
EOF

cat > /etc/systemd/system/shop-backup.service <<'EOF'
[Unit]
Description=Резервная копия базы магазина
OnFailure=shop-alert@%n.service

[Service]
Type=oneshot
User=deploy
ExecStart=/usr/local/bin/shop-backup
EOF

cat > /etc/systemd/system/shop-backup.timer <<'EOF'
[Unit]
Description=Копия базы магазина раз в сутки

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/shop-watch.service <<'EOF'
[Unit]
Description=Сторож магазина: диск, витрина снаружи, свежесть копии

[Service]
Type=oneshot
ExecStart=/usr/local/bin/shop-watch
EOF

cat > /etc/systemd/system/shop-watch.timer <<'EOF'
[Unit]
Description=Сторож магазина каждые четверть часа

[Timer]
OnBootSec=10min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/shop.service <<'EOF'
[Unit]
Description=Mco shop: бот и витрина
After=network-online.target
Wants=network-online.target
# Restart=always поднимает магазин сам; сюда доходит только то, что не
# поднялось совсем, — про это владелец должен узнать сразу.
OnFailure=shop-alert@%n.service

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
systemctl enable --now shop-backup.timer shop-watch.timer >/dev/null
REMOTE
}

code() {
  echo "== код и данные =="
  # «W P» — рабочая папка владельца рядом с кодом: записи экрана и музыка.
  # Без исключения выкладка увозила на сервер 700 мегабайт видео и забивала
  # диск, а сама обрывалась на полпути, не дойдя до витрины.
  rsync -az --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude '.env' --exclude '.env.deploy' --exclude 'data' \
    --exclude '.DS_Store' --exclude '/W P' --exclude '.remember' --exclude 'backups' \
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
