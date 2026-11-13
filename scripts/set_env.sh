#!/usr/bin/env bash
# Put one secret into .env without it ever appearing on the screen or in the shell history.
# Usage (from the project folder):   ./scripts/set_env.sh WHATSAPP_ACCESS_TOKEN
set -eu
key="${1:?usage: set_env.sh KEY_NAME}"
touch .env
read -rsp "Value for $key (paste it, nothing is shown, then press Enter): " value
echo
[ -n "$value" ] || { echo "Nothing entered, $key was not changed."; exit 1; }
sed -i "/^$key=/d" .env
printf '%s=%s\n' "$key" "$value" >> .env
echo "$key saved (${#value} characters)."
