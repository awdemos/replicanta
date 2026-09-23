#!/bin/bash
# Build doom-ascii (https://github.com/wojciech-graj/doom-ascii, GPL-2.0)
# into gitignored .deps/ and fetch the shareware DOOM WAD.
#
# doom-ascii is an optional external component: Replicanta (MIT) does not
# ship its source or binaries. Running this script is the supported way to
# get a playable DOOM. Safe to re-run; existing pieces are skipped.
#
# Supply-chain pinning:
#   - the doom-ascii clone is pinned to a specific commit (no
#     branch-floating --depth 1 of the default branch);
#   - doom1.wad is verified by SHA-256 (the shareware v1.9 file), not byte
#     size. On mismatch the file is deleted and the script fails loudly.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPS="$ROOT/.deps"
REPO_URL="https://github.com/wojciech-graj/doom-ascii"
DOOM_COMMIT="ce9f7eeb14cf1099b2a03007f919086a3a8be1e2"  # known-good upstream rev (v0.3.2-era)
WAD_SHA256=1d7d43be501e67d927e415e0b8f3e29c3bf33075e859721816f652a526cac771  # doom1.wad v1.9 (shareware)

mkdir -p "$DEPS"

wad_ok() {
    [ -s "$WAD" ] || return 1
    echo "$WAD_SHA256  $WAD" | sha256sum -c - >/dev/null 2>&1
}

# -- 1. binary ---------------------------------------------------------------
# v0.3.2+ names the binary doom-ascii; older docs said doom_ascii. Find both.
find_bin() {
    find "$DEPS/doom-ascii" \( -name doom_ascii -o -name doom-ascii \) -type f \
        -path '*/game/*' 2>/dev/null | head -1 || true
}
BIN="$(find_bin)"
if [ -z "${BIN}" ]; then
    if ! command -v cc >/dev/null 2>&1 || ! command -v make >/dev/null 2>&1; then
        echo "error: need a C compiler and make to build doom-ascii" >&2
        echo "       (or set DOOM_ASCII_BIN to an existing binary)" >&2
        exit 1
    fi
    echo "== cloning doom-ascii at ${DOOM_COMMIT}"
    rm -rf "$DEPS/doom-ascii"
    git init -q "$DEPS/doom-ascii"
    git -C "$DEPS/doom-ascii" remote add origin "$REPO_URL"
    git -C "$DEPS/doom-ascii" fetch -q --depth 1 origin "$DOOM_COMMIT"
    git -C "$DEPS/doom-ascii" checkout -q FETCH_HEAD
    echo "== building (make)"
    make -C "$DEPS/doom-ascii"
    BIN="$(find_bin)"
fi
[ -x "$BIN" ] || { echo "error: build finished but no doom_ascii binary found" >&2; exit 1; }
echo "binary: $BIN"

# -- 2. WAD ------------------------------------------------------------------
WAD="$DEPS/doom1.wad"
if ! wad_ok; then
    if [ -s "$WAD" ]; then
        echo "error: $WAD exists but its SHA-256 does not match the expected" >&2
        echo "       shareware v1.9 file ($WAD_SHA256) — deleting it." >&2
        rm -f "$WAD"
        exit 1
    fi
    echo "== fetching shareware doom1.wad (sha256-verified)"
    fetch=0
    for url in \
        "https://raw.githubusercontent.com/Akbar30Bill/DOOM_wads/master/doom1.wad" \
        ; do
        echo "   trying $url"
        if curl -fL --connect-timeout 10 -o "$WAD.download" "$url" 2>/dev/null; then
            # the doomworld mirror is a zip; unpack if so
            if file "$WAD.download" | grep -qi zip; then
                unzip -o -j "$WAD.download" "*.wad" -d "$DEPS" >/dev/null 2>&1 || true
                rm -f "$WAD.download"
            else
                mv "$WAD.download" "$WAD"
            fi
        fi
        if wad_ok; then
            fetch=1
            break
        fi
        rm -f "$WAD.download" "$WAD"
    done
    if [ "$fetch" != 1 ]; then
        echo "error: could not download a SHA-256-verified shareware WAD from the mirror list." >&2
        echo "       place a doom1.wad v1.9 at $WAD" >&2
        echo "       or any .wad under ~/.local/share/replicanta/ and re-run." >&2
        echo "       (Freedoom works too: https://github.com/freedoom/freedoom)" >&2
        exit 1
    fi
fi
echo "wad:    $WAD (sha256 ok)"

cat <<EOF

doom-ascii is ready. Replicanta finds it automatically (.deps/ is searched
by default); to be explicit:

    export DOOM_ASCII_BIN="$BIN"
    export DOOM_WAD="$WAD"

Then: .venv/bin/replicanta, /doom start
EOF
