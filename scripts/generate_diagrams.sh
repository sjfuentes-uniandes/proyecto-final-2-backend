#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIAGRAM_DIR="${ROOT_DIR}/docs/arquitectura"
OUTPUT_DIR="${DIAGRAM_DIR}/generated"

mkdir -p "${OUTPUT_DIR}"

docker run --rm \
  -e PLANTUML_LIMIT_SIZE=16384 \
  -v "${ROOT_DIR}:/work" \
  -w /work \
  plantuml/plantuml:latest \
  -tsvg \
  -tpng \
  -o /work/docs/arquitectura/generated \
  /work/docs/arquitectura/*.puml

printf '\nDiagramas generados en: %s\n' "${OUTPUT_DIR}"
