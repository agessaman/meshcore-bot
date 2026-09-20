#!/usr/bin/env bash
# Rewrite absolute shebangs in a virtualenv's bin/ scripts after the tree has
# been relocated.  `python -m venv` and pip bake the creation path into console
# scripts; an atomic mv from .venv-build-$$ to venv/ leaves those shebangs
# pointing at a path that no longer exists ("cannot execute: required file not
# found").  Symlinked interpreters (python, python3, …) are left alone.
#
# Usage: rewrite_venv_shebangs.sh /path/to/venv
set -euo pipefail

venv="${1:?usage: $0 /path/to/venv}"
new_python="$venv/bin/python"

if [ ! -x "$new_python" ]; then
    echo "rewrite_venv_shebangs: no usable interpreter at $new_python" >&2
    exit 1
fi

shopt -s nullglob
for script in "$venv/bin"/*; do
    [ -f "$script" ] || continue
    [ -L "$script" ] && continue

    first="$(head -n 1 "$script" 2>/dev/null || true)"
    case "$first" in
        "#!"*"python"*)
            if [ "$first" = "#!$new_python" ]; then
                continue
            fi
            {
                printf '#!%s\n' "$new_python"
                tail -n +2 "$script"
            } >"$script.tmp"
            mv "$script.tmp" "$script"
            chmod a+rx "$script"
            ;;
    esac
done
