#!/usr/bin/env bash
set -euo pipefail

: "${GSPREAD_CREDENTIALS_JSON:?missing}"
: "${GSPREAD_AUTHORIZED_USER_JSON:?missing}"
: "${LINE_CHANNEL_ACCESS_TOKEN:?missing}"

mkdir -p "$HOME/.gspread" "$HOME/.line"
printf '%s' "$GSPREAD_CREDENTIALS_JSON" > "$HOME/.gspread/credentials.json"
printf '%s' "$GSPREAD_AUTHORIZED_USER_JSON" > "$HOME/.gspread/authorized_user.json"
printf 'LINE_CHANNEL_ACCESS_TOKEN=%s\n' "$LINE_CHANNEL_ACCESS_TOKEN" > "$HOME/.line/golden-peach-oa.env"
chmod 600 "$HOME/.gspread/"* "$HOME/.line/"*

exec python check_inventory.py --push
