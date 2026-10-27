#!/bin/bash
set -e

cd "/Users/duygu_mac/Projects/DuyguAI-Automation-v0_2"

# shellcheck disable=SC1091
. "$PWD/macos_python_env.sh"

echo "=== DuyguAI Automation — Test Koşumu ==="
echo "1. pytest çalıştırılıyor..."
.venv313/bin/pytest -v

echo ""
echo "2. unittest suite çalıştırılıyor..."
.venv313/bin/python -m unittest discover -s tests -v

echo ""
echo "Tüm testler başarıyla tamamlandı!"
