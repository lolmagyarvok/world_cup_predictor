#!/bin/bash

# ==============================================================================
# run_daily.sh – Napi automatizált VB pipeline
# 
# Cron beállítás (minden reggel 8:00, logolás a projekt saját mappájába!):
#   0 8 * * * /path/to/your/project/run_daily.sh >> /path/to/your/project/logs/pipeline.log 2>&1
# ==============================================================================

# Szigorú Bash mód: 
# -e: Azonnal leáll, ha bármelyik parancs hibával tér vissza (nem kell if [ $? -ne 0 ])
# -u: Leáll, ha nem létező változót próbálsz használni
# -o pipefail: Pipeline-ok esetén sem nyeli le a hibát
set -euo pipefail

# 1. Munkakönyvtár beállítása (a szkript saját mappája, azaz a projekt gyökere)
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# 2. Log mappa ellenőrzése / létrehozása
mkdir -p "$PROJECT_DIR/logs"

# 3. Virtual Environment Pythonjának megkeresése
if [ -f "$PROJECT_DIR/venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
elif [ -f "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
elif [ -f "$PROJECT_DIR/venv/Scripts/python.exe" ]; then
    PYTHON_BIN="$PROJECT_DIR/venv/Scripts/python.exe"
elif [ -f "$PROJECT_DIR/.venv/Scripts/python.exe" ]; then
    # BINGÓ: Itt fogja megtalálni a te Windowsos .venv mappádat!
    PYTHON_BIN="$PROJECT_DIR/.venv/Scripts/python.exe"
else
    echo "❌ KRITIKUS HIBA: Nem található a virtuális környezet (venv) Python futtatható állománya!"
    echo "Keresett útvonalak: venv/bin, .venv/bin, venv/Scripts, .venv/Scripts"
    exit 1
fi
# ==============================================================================
# FŐ LOGIKA
# ==============================================================================

echo ""
echo "======================================================"
echo "  🏆 VB NAPI PIPELINE  –  $(date '+%Y-%m-%d %H:%M:%S')"
echo "======================================================"

echo ""
echo "  [1/3] Eredmények frissítése..."
"$PYTHON_BIN" scripts/update_results.py

echo ""
echo "  [2/3] Napi kiértékelés (ROI, pontosság)..."
"$PYTHON_BIN" scripts/evaluator_daily.py

echo ""
echo "  [3/3] Mai predikciók generálása..."
"$PYTHON_BIN" scripts/daily_predictor.py

echo ""
echo "======================================================"
echo "  ✅ Pipeline sikeresen lefutott: $(date '+%H:%M:%S')"
echo "======================================================"