#!/usr/bin/env bash
# support-twin quickstart installer (Linux / macOS / Git Bash on Windows).
#   ./scripts/install.sh            # core + tests
#   ./scripts/install.sh --live     # also the real-integration SDKs (Telegram, Gmail, Groq, ...)
# Creates .venv, installs the package in editable mode, copies the example config into ./.twin and .env.
# Never overwrites a filled config. Never asks for or stores a secret.
set -euo pipefail

cd "$(dirname "$0")/.."
EXTRAS="test"
[[ "${1:-}" == "--live" ]] && EXTRAS="test,live"

ok() { "$@" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; }
PY=()
if [[ -n "${PYTHON:-}" ]]; then
  PY=("$PYTHON")
else   # the first interpreter that really is 3.12+ (skips e.g. the Windows Store "python3" stub)
  for c in python3.13 python3.12 python3 python "py -3.12"; do
    read -r -a cand <<<"$c"
    if command -v "${cand[0]}" >/dev/null 2>&1 && ok "${cand[@]}"; then PY=("${cand[@]}"); break; fi
  done
fi
[[ ${#PY[@]} -gt 0 ]] && ok "${PY[@]}" || { echo "Python 3.12+ is required (set PYTHON=/path/to/python)" >&2; exit 1; }
echo "== using $("${PY[@]}" -V 2>&1)"

echo "== venv (.venv)"
[[ -d .venv ]] || "${PY[@]}" -m venv .venv
if [[ -x .venv/bin/python ]]; then VPY=.venv/bin/python; else VPY=.venv/Scripts/python.exe; fi

echo "== pip install -e .[$EXTRAS]"
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -e ".[$EXTRAS]"

echo "== config"
mkdir -p .twin/config .twin/data
[[ -f .twin/config/config.yaml ]] && echo "keep existing .twin/config/config.yaml" \
  || cp config.example.yaml .twin/config/config.yaml
[[ -f .env ]] && echo "keep existing .env" || cp .env.example .env

echo "== guard self-test"
TWIN_AGENT_HOME="$PWD/.twin" "$VPY" -m twin_agent.cli guards-scan --selftest

cat <<EOF

Done. Next:
  source .venv/bin/activate        # Windows Git Bash: source .venv/Scripts/activate
  support-twin demo                # one synthetic ticket end to end, nothing leaves this machine
  \$EDITOR .twin/config/config.yaml # set your persona under 'profile:'
  python -m pytest -q              # the test-suite
EOF
