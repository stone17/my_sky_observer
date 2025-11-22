#!/bin/bash

set -e

echo "--- My Sky Observer Installer ---"

echo "[1/3] Installing Python Dependencies..."
pip install -r requirements.txt

echo "[2/3] Installing Frontend Dependencies..."
cd frontend
npm install

echo "[3/3] Building Frontend..."
npm run build

echo "--- Installation Complete ---"
echo "Run with: uvicorn backend.main:app --reload"
