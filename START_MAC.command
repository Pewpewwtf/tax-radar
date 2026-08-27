#!/bin/bash
set -e
cd "$(dirname "$0")"
echo "Tax Radar CLOUD 1.3: запуск..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "Не найден Python 3. Установите Python 3 с python.org и запустите файл снова."
  read -p "Нажмите Enter для выхода..."
  exit 1
fi
if [ ! -d ".venv" ]; then python3 -m venv .venv; fi
source .venv/bin/activate
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
echo "Tax Radar запускается: http://127.0.0.1:8794/?v=cloud-1.3"
python run_local.py
