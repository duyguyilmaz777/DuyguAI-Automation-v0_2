#!/bin/bash
set -e

cd "/Users/duygu_mac/Projects/DuyguAI-Automation-v0_2"

# Homebrew python@3.13 pyexpat / sistem libexpat uyumsuzluğu (platform.mac_ver boş).
# shellcheck disable=SC1091
. "$PWD/macos_python_env.sh"

PROFILE="$HOME/Chrome-Automation-Profile"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PYTHON="$PWD/.venv313/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "HATA: Python 3.13 ortamı bulunamadı: $PYTHON"
    read -r -p "Çıkmak için Enter'a basın..."
    exit 1
fi

if [ ! -x "$CHROME" ]; then
    echo "HATA: Google Chrome bulunamadı."
    read -r -p "Çıkmak için Enter'a basın..."
    exit 1
fi

GEM_URL=$("$PYTHON" -c 'import json; print(json.load(open("config.json"))["gem_url"])')

if ! /usr/bin/curl -fsS http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
    "$CHROME" \
        --remote-debugging-port=9222 \
        --remote-allow-origins='*' \
        --user-data-dir="$PROFILE" \
        --no-first-run \
        "$GEM_URL" >/dev/null 2>&1 &
    sleep 3
fi

exec "$PYTHON" app.py
