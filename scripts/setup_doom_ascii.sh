#!/bin/bash
# Build doom-ascii (https://github.com/wojciech-graj/doom-ascii, GPL-2.0)
# into gitignored .deps/ and fetch the shareware DOOM WAD.
#
# doom-ascii is an optional external component: Replicanta (MIT) does not
# ship its source or binaries. Running this script is the supported way to
# get a playable DOOM. Safe to re-run; existing pieces are skipped.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPS="$ROOT/.deps"
REPO_URL="https://github.com/wojciech-graj/doom-ascii"
WAD_SIZE=4196020  # shareware doom1.wad, bytes

mkdir -p "$DEPS"

# -- 1. binary ---------------------------------------------------------------
BIN="$(find "$DEPS/doom-ascii" -path '*/game/doom_ascii' -type f 2>/dev/null | head -1 || true)"
if [ -z "${BIN}" ]; then
    if ! command -v cc >/dev/null 2>&1 || ! command -v make >/dev/null 2>&1; then
        echo "error: need a C compiler and make to build doom-ascii" >&2
        echo "       (or set DOOM_ASCII_BIN to an existing binary)" >&2
        exit 1
    fi
    echo "== cloning doom-ascii into $DEPS/doom-ascii"
    git clone --depth 1 "$REPO_URL" "$DEPS/doom-ascii"
    echo "== building (make)"
    make -C "$DEPS/doom-ascii"
    BIN="$(find "$DEPS/doom-ascii" -path '*/game/doom_ascii' -type f | head -1)"
fi
[ -x "$BIN" ] || { echo "error: build finished but no doom_ascii binary found" >&2; exit 1; }
echo "binary: $BIN"

# -- 2. WAD ------------------------------------------------------------------
WAD="$DEPS/doom1.wad"
if [ ! -s "$WAD" ] || [ "$(stat -c%s "$WAD")" != "$WAD_SIZE" ]; then
    echo "== fetching shareware doom1.wad ($WAD_SIZE bytes)"
    rm -f "$WAD"
    fetch=0
    for url in \
        "https://distro.ibiblio.org/slitaz/sources/packages/doom1.wad" \
        "https://www.doomworld.com/3ddownloads/ports/shareware_doom_iwad.zip" \
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
        if [ -s "$WAD" ] && [ "$(stat -c%s "$WAD")" = "$WAD_SIZE" ]; then
            fetch=1
            break
        fi
        rm -f "$WAD.download" "$WAD"
    done
    if [ "$fetch" != 1 ]; then
        echo "error: could not download the shareware WAD from the mirror list." >&2
        echo "       place a doom1.wad (exactly $WAD_SIZE bytes) at $WAD" >&2
        echo "       or any .wad under ~/.local/share/replicanta/ and re-run." >&2
        exit 1
    fi
fi
echo "wad:    $WAD"

cat <<EOF

doom-ascii is ready. Replicanta finds it automatically (.deps/ is searched
by default); to be explicit:

    export DOOM_ASCII_BIN="$BIN"
    export DOOM_WAD="$WAD"

Then: .venv/bin/replicanta, /doom start
EOF
