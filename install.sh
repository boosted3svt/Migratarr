#!/usr/bin/env bash
set -euo pipefail

installer_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if ! command -v python3 >/dev/null 2>&1; then
    if [[ " $* " == *" --no-system-packages "* ]]; then
        echo "Migratarr requires Python 3.8+. Install python3, then retry." >&2
        exit 1
    fi
    if ! grep -Eq '^ID="?ubuntu"?$' /etc/os-release; then
        echo "The installer currently supports Ubuntu destinations." >&2
        exit 1
    fi
    if [[ "$(id -u)" == 0 ]]; then
        apt-get update
        apt-get install -y --no-install-recommends --no-upgrade python3
    else
        sudo apt-get update
        sudo apt-get install -y --no-install-recommends --no-upgrade python3
    fi
fi
exec python3 "$installer_root/scripts/install.py" "$@"
