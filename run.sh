#!/usr/bin/env bash
# Mercado Asistente — lanzador del servidor local (HTTPS sobre 127.0.0.1).
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
HOST="127.0.0.1"
PORT="8443"
CERT="certs/cert.pem"
KEY="certs/key.pem"

# 1) Entorno virtual + dependencias
if [ ! -d "$VENV" ]; then
  echo "→ Creando entorno virtual…"
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# 2) Certificado autofirmado para HTTPS local (necesario para el redirect OAuth)
if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
  echo "→ Generando certificado autofirmado…"
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY" -out "$CERT" -days 825 \
    -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1
fi

# 3) Lanzar servidor
echo "→ Servidor en https://localhost:${PORT}"
exec uvicorn main:app \
  --app-dir backend \
  --host "$HOST" --port "$PORT" \
  --ssl-keyfile "$KEY" --ssl-certfile "$CERT"
