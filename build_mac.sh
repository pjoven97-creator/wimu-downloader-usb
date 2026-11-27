#!/bin/bash
# Ejecutar en un Mac, dentro de esta carpeta:  bash build_mac.sh
# Requiere Python 3.12+ de python.org (incluye tkinter).
set -e
cd "$(dirname "$0")"
python3 -m pip install pyinstaller
python3 -m PyInstaller --windowed --noconfirm --name "WIMU_Downloader" --icon icon.icns wimu_downloader.py
# comprimir para poder compartirla
ditto -c -k --sequesterRsrc --keepParent "dist/WIMU_Downloader.app" "dist/WIMU_Downloader_mac.zip"
echo
echo "Listo: dist/WIMU_Downloader.app  (y dist/WIMU_Downloader_mac.zip)"
