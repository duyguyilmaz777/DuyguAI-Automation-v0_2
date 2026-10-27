#!/bin/bash
cd "$(dirname "$0")"

echo "=========================================================="
echo "🚀 DuyguAI Studio & İkizler Masal Durağı Web Studio Başlatılıyor"
echo "=========================================================="

if [ -f "./macos_python_env.sh" ]; then
    . "./macos_python_env.sh"
fi

# 1.5 saniye sonra varsayılan tarayıcıda aç
(sleep 1.5 && open "http://localhost:8765") &

# Sunucuyu başlat
if [ -d ".venv313" ]; then
    .venv313/bin/python web_server.py 8765
else
    python3 web_server.py 8765
fi
