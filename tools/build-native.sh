#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
project=$PWD
mkdir -p build .cache
revision=$(python3 -c 'import json; print(json.load(open("deps.lock.json"))["ghostty"]["revision"])')
prefix=$project/build/ghostty
patch_digest=$(python3 - <<'PY'
import hashlib, pathlib
digest = hashlib.sha256()
for path in sorted(pathlib.Path('patches').glob('*.patch')):
    digest.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
print(digest.hexdigest())
PY
)

if [[ ! -f $prefix/revision || $(<"$prefix/revision") != "$revision" || ! -f $prefix/patches.sha256 || $(<"$prefix/patches.sha256") != "$patch_digest" || ! -f $prefix/include/ghostty/vt.h || ! -f $prefix/lib/libghostty-vt.so ]]; then
    zig=${ZIG:-zig}
    if ! command -v "$zig" >/dev/null; then
        zig=$(python3 tools/zig.py)
    fi
    [[ $("$zig" version) == 0.16.0 ]] || { printf '%s\n' 'Batty requires Zig 0.16.0 for the pinned Ghostty API.' >&2; exit 1; }
    source_dir=${GHOSTTY_SOURCE:-$project/.cache/ghostty}
    if [[ -n ${GHOSTTY_SOURCE:-} ]]; then
        [[ -e $source_dir/.git ]] || { printf '%s\n' 'GHOSTTY_SOURCE must be an existing Ghostty checkout containing the pinned revision.' >&2; exit 1; }
    else
        if [[ ! -e $source_dir/.git ]]; then
            git init -q "$source_dir"
            git -C "$source_dir" remote add origin https://github.com/ghostty-org/ghostty.git
        fi
        if ! git -C "$source_dir" cat-file -e "$revision^{commit}" 2>/dev/null; then
            git -C "$source_dir" fetch --depth 1 origin "$revision"
        fi
    fi
    git -C "$source_dir" cat-file -e "$revision^{commit}"
    build_source=$project/.cache/ghostty-$revision
    mkdir -p "$build_source"
    git -C "$source_dir" archive "$revision" | tar -x -C "$build_source"
    for patch_file in "$project"/patches/*.patch; do
        [[ -f $patch_file ]] || continue
        git -C "$build_source" apply -- "$patch_file"
    done
    ghostty_staging=$(mktemp -d "$project/build/.ghostty.XXXXXX")
    trap 'rm -rf -- "$ghostty_staging"' EXIT
    (cd "$build_source"
     "$zig" build -Demit-lib-vt -Doptimize=ReleaseFast -j"${BUILD_JOBS:-4}" \
        --cache-dir "$project/.cache/zig-local" --global-cache-dir "$project/.cache/zig-global" -p "$ghostty_staging")
    cp "$build_source/LICENSE" "$ghostty_staging/LICENSE"
    printf '%s\n' "$revision" >"$ghostty_staging/revision"
    printf '%s\n' "$patch_digest" >"$ghostty_staging/patches.sha256"
    # Replace complete files so a live terminal can keep its mapped library.
    python3 - "$ghostty_staging" "$prefix" <<'PY'
import os, pathlib, sys
source, target = map(pathlib.Path, sys.argv[1:])
files = [p for p in source.rglob('*') if not p.is_dir()]
for path in sorted(files, key=lambda p: (p.name == 'patches.sha256', p.is_symlink(), str(p))):
    destination = target / path.relative_to(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == '.pc' and path.parent.name == 'pkgconfig':
        path.write_text(path.read_text().replace(f'prefix={source}\n', f'prefix={target}\n', 1))
    os.replace(path, destination)
PY
    rm -rf -- "$ghostty_staging"
    trap - EXIT
fi

bash_source=${BASH_SOURCE_DIR:?Run ./build.sh to prepare the bash-os runtime and matching headers.}
[[ -f $bash_source/config.h && -f $bash_source/builtins.h ]] || {
    printf '%s\n' 'The managed bash-os build is missing its configured GNU Bash 5.3 headers.' >&2
    exit 1
}
python3 - "$bash_source/version.h" <<'PY'
import pathlib, re, sys
if not re.search(r'#define\s+DISTVERSION\s+"5\.3"', pathlib.Path(sys.argv[1]).read_text()):
    raise SystemExit('The loadable requires GNU Bash 5.3 headers.')
PY
cc=${CC:-cc}
read -ra package_flags <<<"$(pkg-config --cflags --libs sdl2 freetype2 harfbuzz fontconfig glesv2 libpng)"
read -ra png_flags <<<"$(pkg-config --cflags --libs libpng)"
session_sources=(src/session.c src/graphics.c src/sixel.c src/remote.c src/remote_wire.c src/presentation.c)
window_sources=(src/window.c src/render.c src/image_renderer.c)
common=(-std=c11 -O2 -g -Wall -Wextra -Werror -fPIC -I"$prefix/include" -Isrc)
# The ELF loader expands ORIGIN at runtime.
# shellcheck disable=SC2016
link=(-L"$prefix/lib" '-Wl,-rpath,$ORIGIN/ghostty/lib' -lghostty-vt -lutil -lm)
staging=$(mktemp -d "$project/build/.compile.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT
"$cc" "${common[@]}" src/session_helper.c -o "$staging/batty-session"
"$cc" "${common[@]}" src/state_service.c "${session_sources[@]}" \
    "${png_flags[@]}" "${link[@]}" -o "$staging/batty-state"
"$cc" "${common[@]}" -DHAVE_CONFIG_H -I"$bash_source" -I"$bash_source/include" \
    -I"$bash_source/builtins" -I"$bash_source/examples/loadables" \
    -shared src/builtin.c "${session_sources[@]}" "${window_sources[@]}" \
    "${package_flags[@]}" "${link[@]}" -ldl -o "$staging/batty.so"
"$cc" "${common[@]}" tests/session_test.c "${session_sources[@]}" "${png_flags[@]}" "${link[@]}" -o "$staging/session-test"
"$cc" "${common[@]}" tests/window_test.c "${session_sources[@]}" "${window_sources[@]}" \
    "${package_flags[@]}" "${link[@]}" -o "$staging/window-test"
"$cc" "${common[@]}" tests/cursor_test.c "${session_sources[@]}" "${window_sources[@]}" \
    "${package_flags[@]}" "${link[@]}" -o "$staging/cursor-test"
"$cc" "${common[@]}" tests/persistence_test.c "${session_sources[@]}" "${window_sources[@]}" \
    "${package_flags[@]}" "${link[@]}" -o "$staging/persistence-test"
"$cc" "${common[@]}" tests/graphics_test.c "${session_sources[@]}" "${window_sources[@]}" \
    "${package_flags[@]}" "${link[@]}" -o "$staging/graphics-test"
"$cc" "${common[@]}" tests/sixel_test.c src/sixel.c -o "$staging/sixel-test"
# Replace complete files so rebuilding does not truncate a running loadable.
for artifact in batty-session batty-state batty.so session-test window-test cursor-test persistence-test graphics-test sixel-test; do
    mv -f -- "$staging/$artifact" "build/$artifact"
done
printf '%s\n' 'Built build/batty.so, build/batty-state and build/batty-session. Run ./batty.'
