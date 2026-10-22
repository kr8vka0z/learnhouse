#!/usr/bin/env bash
# Writes deploy/local-preview/.env from .env.example, filling every empty
# value with a fresh random secret. Refuses to overwrite an existing .env,
# because the database volume keeps the old Postgres password.
# Usage: ./make-env.sh [port]   (default port 3300)
set -euo pipefail
cd "$(dirname "$0")"

if [ -e .env ]; then
  echo ".env already exists; delete it (and run 'docker compose down -v') to start over." >&2
  exit 1
fi

port="${1:-3300}"
rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }

umask 077
while IFS= read -r line; do
  case "$line" in
    HTTP_PORT=*) echo "HTTP_PORT=$port" ;;
    # Passwords get a fixed prefix so they always contain a letter, digit and symbol.
    *PASSWORD=) echo "${line}Pv-$(rand)" ;;
    [A-Z]*=) echo "${line}$(rand)" ;;
    *) echo "$line" ;;
  esac
done < .env.example > .env

echo "Wrote $(pwd)/.env (port $port)."
