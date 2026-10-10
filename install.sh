#!/usr/bin/env bash
# Already cloned? Execute "bash install.sh" to install or repair GroupGuard.
set -Eeuo pipefail
exec bash "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/scripts/server-manager.sh" install
