#!/usr/bin/env bash
# Запуск магазина для теста одной командой: туннель, свежий адрес витрины, бот.
#
# Telegram пускает в мини-приложение только по HTTPS, а бесплатный туннель даёт
# новый адрес при каждом запуске. Руками это четыре шага: поднять туннель,
# скопировать адрес, вписать его в `.env`, перезапустить процесс — и на каждом
# можно забыть про `/app/` в конце или оставить работать прежнюю версию кода.
# Скрипт делает все четыре сам, поэтому `.env` править больше не нужно.
#
#     ./run.sh          поднять туннель и магазин
#
# Останавливается Ctrl+C: гаснут оба процесса. Порт берётся из `.env`,
# адрес витрины туда же и записывается — чтобы `--check` показывал правду.

set -euo pipefail
cd "$(dirname "$0")"

PYTHON=.venv/bin/python
PORT=$(sed -n 's/^PORT=//p' .env 2>/dev/null | head -1 || true)
PORT=${PORT:-8080}
TUNNEL_LOG=$(mktemp -t mco-tunnel)

# Прежний магазин держит порт и работает на прежнем коде: без этого новая
# версия просто не поднимется, а владелец будет думать, что она запущена.
BUSY=$(lsof -ti "tcp:$PORT" || true)
if [ -n "$BUSY" ]; then
    # Печатаем, что именно гасим: на этом порту может оказаться и не магазин.
    echo "останавливаю на порту $PORT: $(ps -o command= -p "$BUSY" | head -1)"
    kill "$BUSY"
    sleep 1
fi

# Туннель через pinggy: он ходит по 443, а cloudflared требует порт 7844,
# который в домашней сети закрыт — там он висит на «Failed to dial quic»,
# и витрина снаружи отвечает ошибкой 1033.
ssh -n -p 443 \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o ServerAliveInterval=30 \
    -o ExitOnForwardFailure=yes \
    -R "0:localhost:$PORT" a.pinggy.io > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!
trap 'kill $TUNNEL_PID 2>/dev/null || true' EXIT

printf "поднимаю туннель"
for _ in $(seq 40); do
    # Адрес туннеля — это «токен-ip-с-дефисами.домен.pinggy». Дефисы в шаблоне
    # обязательны: pinggy печатает рядом рекламу со ссылкой на dashboard.pinggy.io,
    # и без них в витрину уезжает она.
    ADDRESS=$(tr -d '\r' < "$TUNNEL_LOG" \
        | grep -oE 'https://[a-z0-9]+-[0-9-]+\.[a-z0-9.-]*pinggy[a-z.-]*' | head -1 || true)
    [ -n "$ADDRESS" ] && break
    printf "."
    sleep 1
done
echo

if [ -z "${ADDRESS:-}" ]; then
    echo "туннель не поднялся, вот что он сказал:" >&2
    tail -5 "$TUNNEL_LOG" >&2
    exit 1
fi

WEBAPP_URL="$ADDRESS/app/"

# Адрес и в окружение (его читает запуск ниже), и в файл — чтобы следующий
# ручной `python -m src.app` не ушёл на мёртвый туннель.
export WEBAPP_URL
if grep -q '^WEBAPP_URL=' .env 2>/dev/null; then
    updated=$(mktemp)
    sed "s|^WEBAPP_URL=.*|WEBAPP_URL=$WEBAPP_URL|" .env > "$updated" && mv "$updated" .env
else
    echo "WEBAPP_URL=$WEBAPP_URL" >> .env
fi

echo "витрина: $WEBAPP_URL"
echo "версия:  $(git rev-parse --short HEAD) $(git diff --quiet || echo '+ несохранённые правки')"
echo "бесплатный туннель живёт час — потом просто запусти ./run.sh снова"
echo

# Без exec: иначе ловушка выше пропадёт вместе с оболочкой, и туннель останется
# висеть после Ctrl+C — с ним и занятый порт при следующем запуске.
"$PYTHON" -m src.app
