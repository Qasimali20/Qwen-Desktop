#!/usr/bin/env bash
# Builds the analysis sandbox image (qwen-desktop-sandbox:3): Python, Node.js 22 + TypeScript, bash and common CLI tools.
set -euo pipefail
cd "$(dirname "$0")"
PKGS="pandas numpy scipy statsmodels scikit-learn matplotlib seaborn openpyxl xlrd pyarrow tabulate pytest ruff pyyaml requests beautifulsoup4 lxml sympy jinja2 python-docx python-pptx reportlab pypdf markdown"
ARCH=$(uname -m)   # aarch64 on DGX Spark, x86_64 elsewhere
rm -rf site
uv pip install --target site --python-version 3.12 --python-platform "${ARCH}-manylinux_2_28" $PKGS
docker build --network host -t qwen-desktop-sandbox:3 .
