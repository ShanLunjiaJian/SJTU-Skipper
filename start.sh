#!/usr/bin/env bash
# Run the source tree with the installed project dependencies.
#
#   ./start.sh                 # serve on the default host/port
#   ./start.sh --port 9000     # options are forwarded to `serve`
#   ./start.sh check           # plugin topology check
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"

if [[ ! -x $PY ]]; then
  echo "error: $PY not found; create the .venv first (see README)." >&2
  exit 1
fi

if ! command -v bun >/dev/null 2>&1; then
  echo "error: Bun >=1.4.4 is required; enter nix develop (see README)." >&2
  exit 1
fi
bun -e 'if (Bun.semver.satisfies(Bun.version, ">=1.4.4") === false) throw new Error("Bun >=1.4.4 is required")'
if [[ ! -d $ROOT/node_modules/@oh-my-pi/pi-coding-agent ]]; then
  echo "error: runtime dependencies not found; run bun install --frozen-lockfile." >&2
  exit 1
fi

# No subcommand -> serve; leading options -> serve with those options.
if [[ $# -eq 0 ]]; then
  set -- serve
elif [[ ${1#-} != "$1" ]]; then
  set -- serve "$@"
fi

# Put the source tree ahead of any installed copy so edits take effect at once.
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" -m src "$@"
