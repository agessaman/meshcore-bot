#!/usr/bin/env bash
# Shared pip arguments for 32-bit ARM (armv6l / armv7l) installs.
#
# Source this file; do not execute it.  It defines configure_armv7_pip_args,
# which fills ARMV7_PIP_ARGS so callers can:
#
#   configure_armv7_pip_args /path/to/requirements.txt
#   python -m pip install "${ARMV7_PIP_ARGS[@]}" -r /path/to/requirements.txt
#
# On 32-bit ARM this adds the piwheels extra-index-url and, when present,
# -c constraints-armv7.txt so pip resolves to wheels instead of compiling
# (or hitting a listed-but-403 archive URL; see issue #269).  On every other
# architecture ARMV7_PIP_ARGS is empty.  See constraints-armv7.txt.
#
# Used by install-service.sh and the .deb postinst so the two paths cannot
# drift.

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    echo "armv7_pip_args.sh: source this file; do not execute it" >&2
    exit 2
fi

# Intended for callers; empty until configure_armv7_pip_args runs.
# shellcheck disable=SC2034
ARMV7_PIP_ARGS=()

_armv7_pip_info() {
    if [ "$(type -t print_info 2>/dev/null)" = function ]; then
        print_info "$1"
    else
        echo "$1" >&2
    fi
}

_armv7_pip_warn() {
    if [ "$(type -t print_warning 2>/dev/null)" = function ]; then
        print_warning "$1"
    else
        echo "$1" >&2
    fi
}

configure_armv7_pip_args() {
    local requirements="$1"
    local constraints

    ARMV7_PIP_ARGS=()
    case "$(uname -m)" in
        armv6l|armv7l) ;;
        *) return 0 ;;
    esac

    ARMV7_PIP_ARGS+=(--extra-index-url https://www.piwheels.org/simple)
    _armv7_pip_info "32-bit ARM detected; using piwheels prebuilt wheels to avoid on-device compilation"

    constraints="$(dirname "$requirements")/constraints-armv7.txt"
    if [ -f "$constraints" ]; then
        ARMV7_PIP_ARGS+=(-c "$constraints")
    else
        _armv7_pip_warn "constraints-armv7.txt not found next to $requirements"
        _armv7_pip_warn "brotli and ephem will compile from source; this can take a while"
    fi
}
