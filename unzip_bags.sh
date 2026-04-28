#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${SCRIPT_DIR}/20260419_050057"

echo "Output directory: ${OUTPUT_DIR}"
mkdir -p "${OUTPUT_DIR}"

shopt -s nullglob
ZIP_FILES=("${SCRIPT_DIR}"/*.zip)

if [[ ${#ZIP_FILES[@]} -eq 0 ]]; then
    echo "No .zip files found in ${SCRIPT_DIR}"
    exit 1
fi

echo "Found ${#ZIP_FILES[@]} zip file(s). Extracting..."

for zip in "${ZIP_FILES[@]}"; do
    echo "  Extracting: $(basename "${zip}")"
    # -j: junk paths (flatten), -o: overwrite without prompting
    unzip -jo "${zip}" -d "${OUTPUT_DIR}"
done

echo ""
echo "Done. Contents of ${OUTPUT_DIR}:"
ls -lh "${OUTPUT_DIR}"
