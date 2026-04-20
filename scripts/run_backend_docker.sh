#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f config/config.json ]; then
  cp config/config_docker_example.json config/config.json
fi

docker compose up --build backend

