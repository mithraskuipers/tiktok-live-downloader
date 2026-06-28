#!/bin/bash
# =============================================================================
#  TIKTOK LIVE MONITOR — launcher
#
#  Usage:
#    ./start.sh                        # read users.txt (may be empty)
#    ./start.sh username               # monitor one user this session
#    ./start.sh user1 user2 user3 ...  # monitor list this session
#
#  Args are session-only and do NOT modify users.txt.
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "ERROR: Cannot cd to $SCRIPT_DIR"; exit 1; }

SAVE_LOCATION_FILE="save_location.txt"
USERS_FILE="users.txt"
START_PORT=29044
SLEEP_INTERVAL="${SLEEP_INTERVAL:-60}"

clear
echo ""
echo "  TikTok Live Monitor"
echo "  ───────────────────────────────────────"
echo ""

# ── Argument handling ────────────────────────────────────────────────────────
ARG_USERS=()
if (( $# > 0 )); then
    echo "  Session users from arguments:"
    for u in "$@"; do
        echo "    + $u"
        ARG_USERS+=("$u")
    done
    echo ""
fi

# ── Dependency checks ────────────────────────────────────────────────────────
echo "  Checking dependencies..."
echo ""

preflight_ok=true

_check() {
    local label="$1" ok="$2" fix="$3"
    if [[ "$ok" == "1" ]]; then
        printf "  \033[32m[OK]\033[0m %s\n" "$label"
    else
        printf "  \033[31m[X]\033[0m  %s\n        → %s\n" "$label" "$fix"
        preflight_ok=false
    fi
}

# Python ≥ 3.8
py_ver=$(python3 -c "import sys; print(sys.version_info >= (3,8))" 2>/dev/null)
[[ "$py_ver" == "True" ]] \
    && _check "$(python3 --version 2>&1)" 1 "" \
    || _check "Python < 3.8 or not found" 0 "Install Python 3.8+ and ensure python3 is in PATH"

# save_location.txt
[[ -f "$SAVE_LOCATION_FILE" ]] \
    && _check "save_location.txt exists" 1 "" \
    || _check "save_location.txt not found" 0 "Create it with your download folder path on one line"

# Download dir writable
if [[ -f "$SAVE_LOCATION_FILE" ]]; then
    _dl=$(tr -d '[:space:]' < "$SAVE_LOCATION_FILE")
    if [[ -d "$_dl" ]]; then
        [[ -r "$_dl" && -w "$_dl" ]] \
            && _check "Download dir writable  ($_dl)" 1 "" \
            || _check "Download dir not writable  ($_dl)" 0 "chmod u+rw \"$_dl\""
    else
        _check "Download dir does not exist  ($_dl)" 0 "mkdir -p \"$_dl\""
    fi
fi

# Python packages — auto-install if missing
for pkg in flask streamlink; do
    if python3 -c "import $pkg" 2>/dev/null; then
        _check "$pkg" 1 ""
    else
        printf "  \033[33m[--]\033[0m %s not found — installing...\n" "$pkg"
        if python3 -m pip install --quiet "$pkg"; then
            _check "$pkg installed" 1 ""
        else
            _check "$pkg install failed" 0 "pip install $pkg"
        fi
    fi
done

echo ""
if [[ "$preflight_ok" != "true" ]]; then
    echo "  Fix the issues above and re-run."
    echo ""
    exit 1
fi

# ── users.txt — auto-create if missing ──────────────────────────────────────
if [[ ! -f "$USERS_FILE" ]]; then
    echo "# One TikTok username per line. Lines starting with # are ignored." > "$USERS_FILE"
    printf "  \033[32m[OK]\033[0m Created empty users.txt\n"
fi

# ── Resolve user list ────────────────────────────────────────────────────────
declare -a USERS=()

if (( ${#ARG_USERS[@]} > 0 )); then
    USERS=("${ARG_USERS[@]}")
else
    mapfile -t USERS < <(grep -v '^\s*#' "$USERS_FILE" | grep -v '^\s*$' | awk '{print tolower($1)}')
    if (( ${#USERS[@]} == 0 )); then
        echo "  users.txt is empty — starting with no users."
        echo "  Add users via the web UI after it opens."
        echo ""
    else
        echo "  Users from users.txt: ${USERS[*]}"
        echo ""
    fi
fi

# ── Runtime dir ──────────────────────────────────────────────────────────────
RUNTIME_DIR=$(mktemp -d /tmp/tktm_XXXXXX)
export RUNTIME_DIR SLEEP_INTERVAL

echo "  Starting..."
echo ""

exec python3 "$SCRIPT_DIR/ui.py" "$RUNTIME_DIR" "$START_PORT" "${USERS[@]}"
