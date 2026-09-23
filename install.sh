#!/usr/bin/env bash
# ============================================================
# elegoo-notify — script di installazione
# - crea .env e config.json dagli example (se assenti)
# - crea le cartelle dati
# - build + avvio del container (network host)
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\e[1;32m[install]\e[0m %s\n' "$*"; }

command -v docker >/dev/null || { echo "docker non trovato"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose v2 non trovato"; exit 1; }

if [ ! -f .env ]; then
    cp .env.example .env
    say "Creato .env — INSERISCI token/chat_id Telegram:"
    say "   nano .env"
else
    say ".env già presente"
fi

if [ ! -f config.json ]; then
    cp config.json.example config.json
    say "Creato config.json (stampante 192.168.1.56)"
else
    say "config.json già presente"
fi

mkdir -p data/logs data/snapshots data/gcodes
say "Cartelle dati pronte (data/{logs,snapshots,gcodes})"

if grep -q "TELEGRAM_TOKEN=$" .env 2>/dev/null || ! grep -q "^TELEGRAM_TOKEN=..*" .env; then
    say "ATTENZIONE: TELEGRAM_TOKEN non impostato in .env (le notifiche resteranno mute)"
fi

say "Build e avvio del container..."
docker compose up --build -d

say "Attendo l'health check..."
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8766/health >/dev/null 2>&1; then
        say "Servizio UP su http://127.0.0.1:8766"
        say "Dashboard:   http://127.0.0.1:8766/"
        say "Stato:        curl http://127.0.0.1:8766/status"
        say "Log:          docker logs -f elegoo-notify"
        exit 0
    fi
    sleep 2
done
echo "Il servizio non risponde su /health — controlla: docker logs elegoo-notify"
exit 1
