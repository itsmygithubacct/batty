#!/usr/bin/env bash
# Install the exact Kilix Voice closure selected by this Kilix checkout.
set -euo pipefail
umask 077

KILIX_HOME="${KILIX_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
GPU_TERMINAL_SOURCE_HOME="${GPU_TERMINAL_SOURCE_HOME:-$HOME/.local/gpu_terminal/sources}"
GPU_TERMINAL_HOME="${GPU_TERMINAL_HOME:-$HOME/.local/gpu_terminal}"
KILIX_STORAGE_HOME="${KILIX_STORAGE_HOME:-$GPU_TERMINAL_HOME/kilix}"
KILIX_STATE_DIRECTORY="${KILIX_STATE_DIRECTORY:-$KILIX_STORAGE_HOME/state}"
KILIX_DATA_HOME="${KILIX_DATA_HOME:-$KILIX_STORAGE_HOME/data}"
KILIX_VOICE_PREFIX="${KILIX_VOICE_PREFIX:-$HOME/.local}"

# Kilix Voice is part of Kilix's source closure. Plebian-OS pins the parent
# Kilix commit, so every network-fetched voice input is transitive and immutable
# without adding another independently coordinated release ref.
#
# The repository commit is pinned below; the coordinated release gate separately
# requires that exact object to be reachable from the configured public remote.
# Dictation uses the official Vosk 0.3.45 x86_64 wheel from PyPI: the wheel
# itself is verified, then only its fixed vosk/libvosk.so member is extracted.
# The release image is x86_64; other architectures can still install the
# read-aloud-only closure.
KILIX_VOICE_REPO="${KILIX_VOICE_REPO:-https://github.com/itsmygithubacct/kilix-voice.git}"
KILIX_VOICE_REF="${KILIX_VOICE_REF:-f501409a82bf73b738b14986e12441bce23ec1c6}"
KILIX_VOICE_LIB_VERSION="${KILIX_VOICE_LIB_VERSION:-0.3.45}"
KILIX_VOICE_LIB_SHA256="${KILIX_VOICE_LIB_SHA256:-25e025093c4399d7278f543568ed8cc5460ac3a4bf48c23673ace1e25d26619f}"
KILIX_VOICE_LIB_URL="${KILIX_VOICE_LIB_URL:-https://files.pythonhosted.org/packages/fc/ca/83398cfcd557360a3d7b2d732aee1c5f6999f68618d1645f38d53e14c9ff/vosk-0.3.45-py3-none-manylinux_2_12_x86_64.manylinux2010_x86_64.whl}"
KILIX_VOICE_LIB_MEMBER=vosk/libvosk.so
KILIX_VOICE_APACHE_LICENSE_FILE="${KILIX_VOICE_APACHE_LICENSE_FILE:-/usr/share/common-licenses/Apache-2.0}"
# The acoustic models are upstream's, published with no signature and no
# checksum file. The small-model digest comes from two independent fetches a
# week apart that agreed; the lgraph digest agrees across two independently
# published archive mirrors whose byte size matches upstream. A mismatch means
# upstream replaced an artifact in place, which is a finding rather than a
# reason to install it anyway.
KILIX_VOICE_MODEL_URL="${KILIX_VOICE_MODEL_URL:-https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip}"
KILIX_VOICE_MODEL_SHA256="${KILIX_VOICE_MODEL_SHA256:-30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498}"
KILIX_VOICE_SMALL_MODEL_URL="$KILIX_VOICE_MODEL_URL"
KILIX_VOICE_SMALL_MODEL_SHA256="$KILIX_VOICE_MODEL_SHA256"
KILIX_VOICE_LGRAPH_MODEL_URL="${KILIX_VOICE_LGRAPH_MODEL_URL:-https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip}"
KILIX_VOICE_LGRAPH_MODEL_SHA256="${KILIX_VOICE_LGRAPH_MODEL_SHA256:-d9838b4aaa82a75c4a17f5aca300eaca129aaab2a7cbf951bafbb500eb9c4334}"

# The catalog ids are the shared KILIX_VOICE_STT_MODEL vocabulary; archive names
# are upstream's. No model is chosen implicitly by a settings screen. This
# installer's no-argument form retains the historical small-en-us default for
# provisioning and `kilix voice install` compatibility, while --model is the
# explicit lazy-install path used by kilix-stt and the desktop surfaces.
model_id=small-en-us
model_archive_directory=vosk-model-small-en-us-0.15
model_title="Vosk small US English acoustic model"

die() { printf 'kilix voice: %s\n' "$*" >&2; exit 1; }
log() { printf 'kilix voice: %s\n' "$*" >&2; }

usage() {
  cat <<'EOF'
usage: install-kilix-voice.sh [--force] [--model MODEL] [--without-dictation] [--print-refs]

  --force              reinstall even when the verified closure is current
  --model MODEL        install small-en-us or lgraph-en-us (default: small-en-us)
  --without-dictation  install read-aloud only, skipping libvosk and the model
  --print-refs         print the immutable source closure without changing anything
EOF
}

force=0
without_dictation=0
model_was_requested=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force) force=1; shift ;;
    --model)
      [ $# -ge 2 ] || { usage >&2; exit 2; }
      model_id="$2"
      model_was_requested=1
      shift 2 ;;
    --without-dictation) without_dictation=1; shift ;;
    --print-refs) print_refs=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

case "$model_id" in
  small-en-us)
    KILIX_VOICE_MODEL_URL="$KILIX_VOICE_SMALL_MODEL_URL"
    KILIX_VOICE_MODEL_SHA256="$KILIX_VOICE_SMALL_MODEL_SHA256"
    model_archive_directory=vosk-model-small-en-us-0.15
    model_title="Vosk small US English acoustic model"
    ;;
  lgraph-en-us)
    KILIX_VOICE_MODEL_URL="$KILIX_VOICE_LGRAPH_MODEL_URL"
    KILIX_VOICE_MODEL_SHA256="$KILIX_VOICE_LGRAPH_MODEL_SHA256"
    model_archive_directory=vosk-model-en-us-0.22-lgraph
    model_title="Vosk US English dynamic-graph acoustic model"
    ;;
  *) die "unknown speech model '$model_id'; choose small-en-us or lgraph-en-us" ;;
esac
[ "$without_dictation" = 0 ] || [ "$model_was_requested" = 0 ] \
  || die "--model and --without-dictation cannot be used together"

if [ "${print_refs:-0}" = 1 ]; then
  # Deliberately before validation, so release tooling can inspect the whole
  # immutable lazy-install catalog without changing the machine.
  printf '%s\n' \
    "kilix-voice=$KILIX_VOICE_REF" \
    "libvosk=$KILIX_VOICE_LIB_VERSION" \
    "libvosk-sha256=$KILIX_VOICE_LIB_SHA256" \
    "model-small-en-us=$KILIX_VOICE_SMALL_MODEL_SHA256" \
    "model-lgraph-en-us=$KILIX_VOICE_LGRAPH_MODEL_SHA256"
  exit 0
fi
[ "$(id -u)" -ne 0 ] || die "run this installer as the desktop user, not root"

[ "$KILIX_VOICE_REF" != unset ] \
  || die "KILIX_VOICE_REF is unset: kilix-voice has no published commit to pin yet"
[[ "$KILIX_VOICE_REF" =~ ^[0-9a-fA-F]{40}$ ]] \
  || die "KILIX_VOICE_REF must be a full 40-character commit SHA"
if [ "$without_dictation" = 0 ]; then
  [[ "$KILIX_VOICE_LIB_VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] \
    || die "KILIX_VOICE_LIB_VERSION must be a plain release tag"
  [[ "$KILIX_VOICE_LIB_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] \
    || die "KILIX_VOICE_LIB_SHA256 must be a full SHA-256 digest"
  [[ "$KILIX_VOICE_MODEL_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] \
    || die "KILIX_VOICE_MODEL_SHA256 must be a full SHA-256 digest"
  [ -f "$KILIX_VOICE_APACHE_LICENSE_FILE" ] \
    || die "Apache-2.0 license text is required at $KILIX_VOICE_APACHE_LICENSE_FILE"
  case "$(uname -m)" in
    x86_64|amd64) ;;
    *) die "the pinned Vosk wheel supports x86_64 only (--without-dictation still installs read-aloud)" ;;
  esac
fi

normalize_absolute() {
  local value="$1" normalized
  case "$value" in /*) ;; *) return 1 ;; esac
  normalized="$(realpath -m -- "$value" 2>/dev/null)" || return 1
  [ "$normalized" = "$value" ] || return 1
  printf '%s\n' "$normalized"
}

source_home="$(normalize_absolute "$GPU_TERMINAL_SOURCE_HOME")" \
  || die "GPU_TERMINAL_SOURCE_HOME must be a normalized absolute path"
prefix="$(normalize_absolute "$KILIX_VOICE_PREFIX")" \
  || die "KILIX_VOICE_PREFIX must be a normalized absolute path"
state_dir="$(normalize_absolute "$KILIX_STATE_DIRECTORY")" \
  || die "KILIX_STATE_DIRECTORY must be a normalized absolute path"
data_dir="$(normalize_absolute "$KILIX_DATA_HOME")" \
  || die "KILIX_DATA_HOME must be a normalized absolute path"
case "$source_home" in /|"$HOME") die "refusing broad source root: $source_home" ;; esac
case "$prefix" in /|"$HOME") die "refusing broad install prefix: $prefix" ;; esac
case "$data_dir" in /|"$HOME") die "refusing broad data root: $data_dir" ;; esac
# The model and the library are generated inputs, so they land under the Kilix
# data root and never in the source checkout.
case "$data_dir" in "$KILIX_HOME"|"$KILIX_HOME"/*)
  die "refusing to place voice data inside the Kilix source checkout: $data_dir" ;;
esac

voice_data="$data_dir/voice"
library_root="$voice_data/lib"
models_root="$voice_data/models"
runtime_root="$voice_data/runtime"
runtime_generations="$runtime_root/generations"
runtime_current="$runtime_root/current"
prefix_bin="$prefix/bin"
ensure_owned_directory() {
  local directory="$1" label="$2"
  # Check the leaf before mkdir/chmod: both commands otherwise follow a leaf
  # symlink and can mutate a directory outside the configured closure.
  [ ! -L "$directory" ] || die "$label must not be a symlink: $directory"
  mkdir -p -- "$directory" || die "could not create $label: $directory"
  [ -d "$directory" ] && [ ! -L "$directory" ] \
    && [ "$(stat -c '%u' -- "$directory" 2>/dev/null)" = "$(id -u)" ] \
    || die "$label must be a real directory owned by the current user: $directory"
}

# Parent-first ordering means every child mkdir starts from a validated leaf.
ensure_owned_directory "$source_home" "source directory"
ensure_owned_directory "$state_dir" "state directory"
ensure_owned_directory "$data_dir" "data directory"
ensure_owned_directory "$voice_data" "voice data directory"
ensure_owned_directory "$library_root" "voice library directory"
ensure_owned_directory "$models_root" "voice model directory"
ensure_owned_directory "$runtime_root" "voice runtime directory"
ensure_owned_directory "$runtime_generations" "voice runtime generations directory"
ensure_owned_directory "$prefix_bin" "voice command directory"
chmod 0700 -- "$state_dir" "$voice_data" "$library_root" "$models_root" \
  "$runtime_root" "$runtime_generations"
if command -v flock >/dev/null 2>&1; then
  # Lock the already-validated directory itself. Opening a named lock file with
  # shell redirection would follow a pre-planted symlink and truncate its target
  # before flock ever ran.
  exec 9<"$state_dir"
  flock 9
fi

# Keep installer-owned exact checkouts separate from editable sibling projects.
# Versioned paths make an update rollback-safe without resetting or replacing a
# developer's checkout, and an older closure remains available after rollback.
managed_sources="$source_home/.kilix-voice-sources"
ensure_owned_directory "$managed_sources" "managed source directory"
chmod 0700 -- "$managed_sources" 2>/dev/null || true
[ -d "$managed_sources" ] && [ ! -L "$managed_sources" ] \
  && [ "$(stat -c '%u:%a' -- "$managed_sources" 2>/dev/null)" = "$(id -u):700" ] \
  || die "managed source directory must be owned by the current user with mode 0700"
voice_dir="$managed_sources/kilix-voice-$KILIX_VOICE_REF"
model="$models_root/$model_id"
stamp="$state_dir/kilix-voice-install.refs"
if [ "$without_dictation" = 1 ]; then
  library_pin=skipped
  model_pin=skipped
else
  library_pin="$KILIX_VOICE_LIB_VERSION+$KILIX_VOICE_LIB_SHA256"
  model_pin="$KILIX_VOICE_MODEL_SHA256"
  # Payload generations are immutable and their names carry the digest of the
  # archive that produced them.  A pin change therefore cannot reuse an older
  # loadable library/model and then relabel its provenance as the new input.
  library_generation_name="vosk-$KILIX_VOICE_LIB_VERSION-${KILIX_VOICE_LIB_SHA256,,}"
  model_generation_name="$model_archive_directory-${KILIX_VOICE_MODEL_SHA256,,}"
fi
expected_refs="$(printf '%s\n' \
  "kilix-voice=$KILIX_VOICE_REF" \
  "libvosk=$library_pin" \
  "model-$model_id=$model_pin")"
# A read-aloud-only run is a subset of the full closure: when the stamp on
# disk records a full install of any catalogued Vosk model with the same pins,
# that install satisfies this one. Without this, the lazy `kilix voice daemon` path (always
# --without-dictation) and a full install would each see the other's stamp as
# stale — reinstalling the runtime on every daemon start and taking turns
# rewriting the stamp, while telling a user with a working dictation closure
# to "rerun without --without-dictation".
full_refs_available=0
if [ "$without_dictation" = 1 ] \
    && [[ "$KILIX_VOICE_LIB_VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] \
    && [[ "$KILIX_VOICE_LIB_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] \
    && [[ "$KILIX_VOICE_SMALL_MODEL_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] \
    && [[ "$KILIX_VOICE_LGRAPH_MODEL_SHA256" =~ ^[0-9a-fA-F]{64}$ ]]; then
  full_refs_available=1
fi

full_stamp_satisfies_runtime() {
  local catalog_model catalog_sha candidate
  [ "$full_refs_available" = 1 ] && [ -f "$stamp" ] || return 1
  while IFS=' ' read -r catalog_model catalog_sha; do
    candidate="$(printf '%s\n' \
      "kilix-voice=$KILIX_VOICE_REF" \
      "libvosk=$KILIX_VOICE_LIB_VERSION+$KILIX_VOICE_LIB_SHA256" \
      "model-$catalog_model=$catalog_sha")"
    printf '%s\n' "$candidate" | cmp -s - "$stamp" && return 0
  done <<EOF
small-en-us $KILIX_VOICE_SMALL_MODEL_SHA256
lgraph-en-us $KILIX_VOICE_LGRAPH_MODEL_SHA256
EOF
  return 1
}

stamp_satisfies_this_run() {
  [ -f "$stamp" ] || return 1
  printf '%s\n' "$expected_refs" | cmp -s - "$stamp" && return 0
  full_stamp_satisfies_runtime
}

# The dictation closure as the daemon resolves it: the promoted library link
# and the model catalog entry. Read-aloud-only runs use this for truthful
# messaging — they must not instruct a user whose dictation already works to
# reinstall it.
dictation_assets_present() {
  local catalog_model resolved
  [ -f "$library_root/current/libvosk.so" ] || return 1
  for catalog_model in small-en-us lgraph-en-us; do
    # The catalog entry is a symlink into the immutable generations; the
    # payload check wants the generation directory itself.
    resolved="$(realpath -e -- "$models_root/$catalog_model" 2>/dev/null)" \
      || continue
    if model_payload_works "$resolved"; then
      installed_dictation_model="$catalog_model"
      return 0
    fi
  done
  return 1
}

print_library_provenance() {
  printf '%s\n' \
    'Kilix Voice native speech-recognition library' \
    "Upstream: https://github.com/alphacep/vosk-api" \
    "Version: $KILIX_VOICE_LIB_VERSION" \
    "Wheel: $KILIX_VOICE_LIB_URL" \
    "Wheel SHA-256: $KILIX_VOICE_LIB_SHA256" \
    "Extracted member: $KILIX_VOICE_LIB_MEMBER" \
    'License: Apache-2.0 (see LICENSE.Apache-2.0)'
}

print_model_provenance() {
  printf '%s\n' \
    "$model_title" \
    'Upstream catalog: https://alphacephei.com/vosk/models' \
    "Archive: $KILIX_VOICE_MODEL_URL" \
    "Archive SHA-256: $KILIX_VOICE_MODEL_SHA256" \
    "Archive directory: $model_archive_directory" \
    'License: Apache-2.0 (see LICENSE.Apache-2.0)'
}

vosk_library_works() {
  local candidate="$1"
  python3 - "$candidate" <<'PY' >/dev/null 2>&1
import ctypes
import sys

try:
    library = ctypes.CDLL(sys.argv[1])
    for symbol in (
        "vosk_set_log_level",
        "vosk_model_new",
        "vosk_model_free",
        "vosk_recognizer_new",
        "vosk_recognizer_accept_waveform",
        "vosk_recognizer_partial_result",
        "vosk_recognizer_final_result",
        "vosk_recognizer_free",
    ):
        getattr(library, symbol)
except (AttributeError, OSError):
    raise SystemExit(1)
PY
}

vosk_model_works() {
  local library="$1" directory="$2"
  python3 - "$library" "$directory" <<'PY' >/dev/null 2>&1
import ctypes
import os
import sys

try:
    library = ctypes.CDLL(sys.argv[1])
    library.vosk_model_new.argtypes = [ctypes.c_char_p]
    library.vosk_model_new.restype = ctypes.c_void_p
    library.vosk_model_free.argtypes = [ctypes.c_void_p]
    model = library.vosk_model_new(os.fsencode(sys.argv[2]))
    if not model:
        raise RuntimeError("Vosk refused the model")
    library.vosk_model_free(model)
except (AttributeError, OSError, RuntimeError):
    raise SystemExit(1)
PY
}

model_payload_works() {
  local directory="$1" asset
  [ -d "$directory" ] && [ ! -L "$directory" ] || return 1
  for asset in conf/model.conf am/final.mdl; do
    [ -s "$directory/$asset" ] && [ ! -L "$directory/$asset" ] || return 1
  done
}

library_generation_works() {
  local directory="$1" notice license
  [ -d "$directory" ] && [ ! -L "$directory" ] || return 1
  [ -f "$directory/libvosk.so" ] && [ ! -L "$directory/libvosk.so" ] \
    && vosk_library_works "$directory/libvosk.so" || return 1
  notice="$directory/README.kilix-provenance"
  license="$directory/LICENSE.Apache-2.0"
  [ -f "$notice" ] && [ ! -L "$notice" ] \
    && print_library_provenance | cmp -s - "$notice" || return 1
  [ -f "$license" ] && [ ! -L "$license" ] \
    && cmp -s -- "$KILIX_VOICE_APACHE_LICENSE_FILE" "$license"
}

model_generation_works() {
  local directory="$1" notice license
  model_payload_works "$directory" || return 1
  notice="$directory/README.kilix-provenance"
  license="$directory/LICENSE.Apache-2.0"
  [ -f "$notice" ] && [ ! -L "$notice" ] \
    && print_model_provenance | cmp -s - "$notice" || return 1
  [ -f "$license" ] && [ ! -L "$license" ] \
    && cmp -s -- "$KILIX_VOICE_APACHE_LICENSE_FILE" "$license"
}

voice_runtime_works() {
  local tool entry expected library_generation model_directory notice license asset
  [ -L "$runtime_current" ] || return 1
  for tool in kilix-tts kilix-stt kilix-voiced; do
    entry="$prefix_bin/$tool"
    expected="$runtime_current/bin/$tool"
    [ -L "$entry" ] && [ "$(readlink -- "$entry")" = "$expected" ] \
      && [ -x "$entry" ] \
      && "$entry" --version >/dev/null 2>&1 || return 1
  done
  [ "$without_dictation" = 0 ] || return 0

  library_generation="$library_root/$library_generation_name"
  [ -L "$library_root/current" ] \
    && [ "$(readlink -- "$library_root/current")" = "$library_generation_name" ] \
    && [ -d "$library_generation" ] && [ ! -L "$library_generation" ] \
    || return 1
  library_generation_works "$library_generation" || return 1

  model_directory="$models_root/$model_generation_name"
  [ -L "$model" ] && [ "$(readlink -- "$model")" = "$model_generation_name" ] \
    && [ -d "$model_directory" ] && [ ! -L "$model_directory" ] \
    || return 1
  model_generation_works "$model_directory" || return 1
  vosk_model_works "$library_generation/libvosk.so" "$model_directory"
}

if [ "$force" = 0 ] && stamp_satisfies_this_run && voice_runtime_works; then
  log "verified voice closure already installed at $prefix/bin"
  exit 0
fi

for command in git make python3 install; do
  command -v "$command" >/dev/null 2>&1 \
    || die "$command is required (install build-essential, git, and python3)"
done
if [ "$without_dictation" = 0 ]; then
  for command in curl sha256sum unzip; do
    command -v "$command" >/dev/null 2>&1 \
      || die "$command is required to install the pinned speech library and model"
  done
fi

clone_tmp=""
download_tmp=""
library_archive=""
library_extract_tmp=""
runtime_stage=""
uncommitted_generation=""
legacy_generation=""
entry_backup=""
stamp_tmp=""
previous_runtime_target=""
previous_library_target=""
previous_model_target=""
runtime_changed=0
library_link_changed=0
model_link_changed=0
transaction_active=0
transaction_committed=0
declare -a changed_entrypoints=()

ensure_checkout() {
  local label="$1" directory="$2" repository="$3" ref="$4"
  local origin head checkout
  if git -C "$directory" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    origin="$(git -C "$directory" remote get-url origin 2>/dev/null || true)"
    [ "$origin" = "$repository" ] \
      || die "$label checkout has origin '${origin:-missing}', expected '$repository': $directory"
    [ -z "$(git -C "$directory" status --porcelain --untracked-files=normal)" ] \
      || die "$label checkout has local changes; refusing to build an unpinned tree: $directory"
    head="$(git -C "$directory" rev-parse HEAD 2>/dev/null || true)"
    [ "${head,,}" = "${ref,,}" ] \
      || die "$label checkout is at ${head:-unknown}, expected $ref: $directory"
    return 0
  fi
  [ ! -e "$directory" ] \
    || die "$label path exists but is not a Git checkout: $directory"
  clone_tmp="$(mktemp -d "$managed_sources/.clone.XXXXXX")" \
    || die "could not allocate a temporary clone directory"
  checkout="$clone_tmp/checkout"
  log "cloning pinned $label -> $directory"
  git clone --no-checkout -- "$repository" "$checkout" \
    || die "could not clone $label from $repository"
  git -C "$checkout" checkout --detach "$ref" \
    || die "$label commit $ref is unavailable from $repository"
  head="$(git -C "$checkout" rev-parse HEAD)"
  [ "${head,,}" = "${ref,,}" ] || die "$label resolved to the wrong commit"
  mv -- "$checkout" "$directory" || die "could not publish $label checkout"
  rm -rf -- "$clone_tmp"
  clone_tmp=""
}

# Nothing unverified is ever kept: the download lands on a temporary path and is
# only moved into place once its digest matches the pin.
fetch_verified() {
  local url="$1" destination="$2" expected="$3" label="$4"
  download_tmp="$(mktemp "$destination.partial.XXXXXX")" \
    || die "could not allocate a download for $label"
  log "fetching verified $label"
  curl -fL --retry 3 --max-time 900 -o "$download_tmp" -- "$url" \
    || die "could not download $label from $url"
  printf '%s  %s\n' "$expected" "$download_tmp" | sha256sum -c --status \
    || die "checksum mismatch for $label; refusing to install an unverified download"
  chmod 0600 -- "$download_tmp" || die "could not secure the downloaded $label"
  mv -f -- "$download_tmp" "$destination" || die "could not publish $label"
  download_tmp=""
}

# A wheel is a ZIP archive, but this is not a package installation. Extract one
# exact, checksum-covered member into an installer-owned path. The fixed member
# name and exclusive destination make path traversal impossible; duplicate,
# encrypted, symlink, oversized, and wrong-architecture members are rejected.
extract_vosk_library() {
  local archive="$1" destination="$2"
  python3 - "$archive" "$destination" "$KILIX_VOICE_LIB_MEMBER" <<'PY'
import os
import stat
import struct
import sys
import zipfile

archive, destination, member = sys.argv[1:]
maximum_bytes = 128 * 1024 * 1024

try:
    with zipfile.ZipFile(archive) as wheel:
        matches = [info for info in wheel.infolist() if info.filename == member]
        if len(matches) != 1:
            raise ValueError(
                "wheel must contain exactly one %s member (found %d)"
                % (member, len(matches)))
        info = matches[0]
        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if info.is_dir() or file_type not in (0, stat.S_IFREG):
            raise ValueError("wheel member %s is not a regular file" % member)
        if info.flag_bits & 0x1:
            raise ValueError("wheel member %s is encrypted" % member)
        if info.file_size < 20 or info.file_size > maximum_bytes:
            raise ValueError("wheel member %s has an unsafe size" % member)

        with wheel.open(info) as source, open(destination, "xb") as output:
            remaining = info.file_size
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("wheel member %s is truncated" % member)
                output.write(chunk)
                remaining -= len(chunk)
            if source.read(1):
                raise ValueError("wheel member %s exceeds its declared size" % member)

    with open(destination, "rb") as library:
        header = library.read(20)
    if header[:6] != b"\x7fELF\x02\x01":
        raise ValueError("wheel member %s is not a little-endian ELF64 library" % member)
    elf_type, machine = struct.unpack_from("<HH", header, 16)
    if elf_type != 3 or machine != 62:
        raise ValueError("wheel member %s is not an x86_64 shared library" % member)
    os.chmod(destination, 0o600)
except (OSError, ValueError, zipfile.BadZipFile) as error:
    try:
        os.unlink(destination)
    except FileNotFoundError:
        pass
    print("kilix voice: could not extract verified Vosk library: %s" % error,
          file=sys.stderr)
    raise SystemExit(1)
PY
}

write_library_provenance() {
  local directory="$1" notice="$1/README.kilix-provenance"
  local license="$1/LICENSE.Apache-2.0" notice_tmp license_tmp
  notice_tmp="$(mktemp "$directory/.README.kilix-provenance.XXXXXX")" \
    || die "could not stage Vosk library provenance"
  license_tmp="$(mktemp "$directory/.LICENSE.Apache-2.0.XXXXXX")" \
    || { rm -f -- "$notice_tmp"; die "could not stage Vosk library license"; }
  print_library_provenance >"$notice_tmp"
  chmod 0644 -- "$notice_tmp"
  install -m 0644 -- "$KILIX_VOICE_APACHE_LICENSE_FILE" "$license_tmp"
  mv -fT -- "$notice_tmp" "$notice" \
    || { rm -f -- "$notice_tmp" "$license_tmp"; die "could not publish Vosk library provenance"; }
  mv -fT -- "$license_tmp" "$license" \
    || { rm -f -- "$license_tmp"; die "could not publish Vosk library license"; }
}

write_model_provenance() {
  local directory="$1" notice="$1/README.kilix-provenance"
  local license="$1/LICENSE.Apache-2.0" notice_tmp license_tmp
  notice_tmp="$(mktemp "$directory/.README.kilix-provenance.XXXXXX")" \
    || die "could not stage Vosk model provenance"
  license_tmp="$(mktemp "$directory/.LICENSE.Apache-2.0.XXXXXX")" \
    || { rm -f -- "$notice_tmp"; die "could not stage Vosk model license"; }
  print_model_provenance >"$notice_tmp"
  chmod 0644 -- "$notice_tmp"
  install -m 0644 -- "$KILIX_VOICE_APACHE_LICENSE_FILE" "$license_tmp"
  mv -fT -- "$notice_tmp" "$notice" \
    || { rm -f -- "$notice_tmp" "$license_tmp"; die "could not publish Vosk model provenance"; }
  mv -fT -- "$license_tmp" "$license" \
    || { rm -f -- "$license_tmp"; die "could not publish Vosk model license"; }
}

# Promotion is a symlink swap so an interrupted install leaves the previous
# generation live, and so a bad upgrade is one relink back rather than a refetch.
swap_link() {
  local target="$1" link="$2" staged
  staged="$(mktemp "$(dirname "$link")/.promote.XXXXXX")" || return 1
  rm -f -- "$staged" || return 1
  if ! ln -s -- "$target" "$staged"; then
    rm -f -- "$staged"
    return 1
  fi
  if ! mv -fT -- "$staged" "$link"; then
    rm -f -- "$staged"
    return 1
  fi
}

promote() {
  swap_link "$1" "$2" || die "could not promote $2"
}

capture_previous_asset_link() {
  local link="$1" root="$2" variable="$3" label="$4" target
  if [ -L "$link" ]; then
    target="$(readlink -- "$link")"
    [[ "$target" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,191}$ ]] \
      || die "$label link has an unsafe target: $target"
    # Only a real in-root generation is worth restoring. A stale safe basename
    # is repaired on success and removed rather than resurrected on rollback.
    if [ -d "$root/$target" ] && [ ! -L "$root/$target" ]; then
      printf -v "$variable" '%s' "$target"
    fi
  elif [ -e "$link" ]; then
    die "$label path exists but is not a symlink: $link"
  fi
}

capture_previous_asset_links() {
  [ "$without_dictation" = 0 ] || return 0
  capture_previous_asset_link "$library_root/current" "$library_root" \
    previous_library_target "Vosk library current"
  capture_previous_asset_link "$model" "$models_root" \
    previous_model_target "Vosk model catalog"
}

stage_runtime_generation() {
  local tool suffix generation
  runtime_stage="$(mktemp -d "$runtime_generations/.install.XXXXXX")" \
    || die "could not allocate voice runtime staging"
  log "installing the pinned voice engine into a private generation"
  make -B -C "$voice_dir" install PREFIX="$runtime_stage"
  for tool in kilix-tts kilix-stt kilix-voiced; do
    if [ ! -f "$runtime_stage/bin/$tool" ] \
        || [ -L "$runtime_stage/bin/$tool" ] \
        || [ ! -x "$runtime_stage/bin/$tool" ]; then
      die "the voice engine did not stage a regular executable: $tool"
    fi
    "$runtime_stage/bin/$tool" --version >/dev/null 2>&1 \
      || die "the staged voice tool could not start: $tool --version"
  done
  suffix="${runtime_stage##*.}"
  generation="$runtime_generations/kilix-voice-${KILIX_VOICE_REF,,}-$suffix"
  mv -- "$runtime_stage" "$generation" \
    || die "could not publish the staged voice runtime generation"
  runtime_stage=""
  uncommitted_generation="$generation"
}

capture_previous_runtime() {
  local target tool
  if [ -L "$runtime_current" ]; then
    target="$(readlink -- "$runtime_current")"
    [[ "$target" =~ ^generations/[A-Za-z0-9._-]+$ ]] \
      || die "voice runtime current link points outside its generation directory"
    for tool in kilix-tts kilix-stt kilix-voiced; do
      if [ ! -f "$runtime_root/$target/bin/$tool" ] \
          || [ -L "$runtime_root/$target/bin/$tool" ] \
          || [ ! -x "$runtime_root/$target/bin/$tool" ]; then
        die "voice runtime current generation is incomplete: $target"
      fi
    done
    previous_runtime_target="$target"
  elif [ -e "$runtime_current" ]; then
    die "voice runtime current path exists but is not a symlink: $runtime_current"
  fi
}

preserve_legacy_runtime() {
  local tool entry legacy_stage suffix
  [ -z "$previous_runtime_target" ] || return 0
  for tool in kilix-tts kilix-stt kilix-voiced; do
    entry="$prefix_bin/$tool"
    [ -f "$entry" ] && [ -x "$entry" ] || return 0
  done

  legacy_stage="$(mktemp -d "$runtime_generations/.legacy.XXXXXX")" \
    || die "could not allocate legacy voice runtime staging"
  mkdir -p -- "$legacy_stage/bin"
  for tool in kilix-tts kilix-stt kilix-voiced; do
    install -m 0755 -- "$prefix_bin/$tool" "$legacy_stage/bin/$tool" \
      || { rm -rf -- "$legacy_stage"
           die "could not preserve the previous $tool"; }
  done
  suffix="${legacy_stage##*.}"
  legacy_generation="$runtime_generations/legacy-$suffix"
  mv -- "$legacy_stage" "$legacy_generation" \
    || die "could not preserve the previous voice runtime generation"
  promote "generations/${legacy_generation##*/}" "$runtime_current"
  runtime_changed=1
}

prepare_runtime_entrypoints() {
  local tool entry expected staged
  capture_previous_runtime
  preserve_legacy_runtime
  entry_backup="$(mktemp -d "$prefix_bin/.kilix-voice-backup.XXXXXX")" \
    || die "could not allocate voice command rollback state"

  for tool in kilix-tts kilix-stt kilix-voiced; do
    entry="$prefix_bin/$tool"
    expected="$runtime_current/bin/$tool"
    if [ -L "$entry" ] && [ "$(readlink -- "$entry")" = "$expected" ]; then
      continue
    fi
    if [ -e "$entry" ] || [ -L "$entry" ]; then
      { [ -f "$entry" ] || [ -L "$entry" ]; } \
        || die "refusing to replace non-file voice command path: $entry"
      cp -a -- "$entry" "$entry_backup/$tool" \
        || die "could not preserve the previous $tool entrypoint"
    else
      : >"$entry_backup/.missing-$tool"
    fi
    staged="$(mktemp "$prefix_bin/.$tool.XXXXXX")" \
      || die "could not allocate a staged $tool entrypoint"
    rm -f -- "$staged"
    ln -s -- "$expected" "$staged" \
      || { rm -f -- "$staged"; die "could not stage the $tool entrypoint"; }
    mv -fT -- "$staged" "$entry" \
      || { rm -f -- "$staged"; die "could not publish the $tool entrypoint"; }
    changed_entrypoints+=("$tool")
  done
}

rollback_runtime_transaction() {
  local failed=0 restored=1 index tool entry
  if [ "$runtime_changed" = 1 ]; then
    if [ -n "$previous_runtime_target" ]; then
      swap_link "$previous_runtime_target" "$runtime_current" || {
        failed=1
        restored=0
      }
    else
      rm -f -- "$runtime_current" || {
        failed=1
        restored=0
      }
    fi
  fi

  for ((index=${#changed_entrypoints[@]} - 1; index >= 0; index--)); do
    tool="${changed_entrypoints[index]}"
    entry="$prefix_bin/$tool"
    if [ -e "$entry_backup/.missing-$tool" ]; then
      rm -f -- "$entry" || failed=1
    else
      mv -fT -- "$entry_backup/$tool" "$entry" || failed=1
    fi
  done

  if [ "$restored" = 1 ]; then
    [ -z "$uncommitted_generation" ] \
      || rm -rf -- "$uncommitted_generation" || failed=1
    [ -z "$legacy_generation" ] \
      || rm -rf -- "$legacy_generation" || failed=1
  fi
  return "$failed"
}

rollback_asset_links() {
  local failed=0
  if [ "$model_link_changed" = 1 ]; then
    if [ -n "$previous_model_target" ]; then
      swap_link "$previous_model_target" "$model" || failed=1
    else
      rm -f -- "$model" || failed=1
    fi
  fi
  if [ "$library_link_changed" = 1 ]; then
    if [ -n "$previous_library_target" ]; then
      swap_link "$previous_library_target" "$library_root/current" || failed=1
    else
      rm -f -- "$library_root/current" || failed=1
    fi
  fi
  return "$failed"
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  set +e
  if [ "$status" -ne 0 ] && [ "$transaction_active" = 1 ] \
      && [ "$transaction_committed" = 0 ]; then
    rollback_runtime_transaction \
      || log "WARNING: voice runtime rollback was incomplete"
    rollback_asset_links \
      || log "WARNING: voice asset-link rollback was incomplete"
  elif [ "$status" -ne 0 ] && [ -n "$uncommitted_generation" ]; then
    rm -rf -- "$uncommitted_generation"
  fi
  [ -z "$clone_tmp" ] || rm -rf -- "$clone_tmp"
  [ -z "$download_tmp" ] || rm -f -- "$download_tmp"
  [ -z "$library_archive" ] || rm -f -- "$library_archive"
  [ -z "$library_extract_tmp" ] || rm -f -- "$library_extract_tmp"
  [ -z "$runtime_stage" ] || rm -rf -- "$runtime_stage"
  [ -z "$stamp_tmp" ] || rm -f -- "$stamp_tmp"
  [ -z "$entry_backup" ] || rm -rf -- "$entry_backup"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ensure_checkout "Kilix Voice" "$voice_dir" "$KILIX_VOICE_REPO" "$KILIX_VOICE_REF"
stage_runtime_generation

if [ "$without_dictation" = 0 ]; then
  library_generation="$library_root/$library_generation_name"
  if [ -e "$library_generation" ] || [ -L "$library_generation" ]; then
    if [ ! -d "$library_generation" ] || [ -L "$library_generation" ]; then
      die "refusing unsafe Vosk library generation: $library_generation"
    fi
  else
    mkdir -- "$library_generation"
  fi
  chmod 0700 -- "$library_generation"
  [ "$(stat -c '%u:%a' -- "$library_generation" 2>/dev/null)" = "$(id -u):700" ] \
    || die "Vosk library generation must be owned by the current user with mode 0700"

  if [ -e "$library_generation/libvosk.so" ] \
      || [ -L "$library_generation/libvosk.so" ]; then
    [ -f "$library_generation/libvosk.so" ] \
      || die "refusing unsafe Vosk library path: $library_generation/libvosk.so"
  fi
  if ! library_generation_works "$library_generation"; then
    library_archive="$library_generation/.vosk-$KILIX_VOICE_LIB_VERSION.whl"
    fetch_verified "$KILIX_VOICE_LIB_URL" "$library_archive" \
      "$KILIX_VOICE_LIB_SHA256" "Vosk $KILIX_VOICE_LIB_VERSION x86_64 wheel"
    library_extract_tmp="$(mktemp "$library_generation/.libvosk.so.XXXXXX")" \
      || die "could not allocate Vosk library extraction staging"
    rm -f -- "$library_extract_tmp"
    extract_vosk_library "$library_archive" "$library_extract_tmp" \
      || die "the verified Vosk wheel did not contain a safe libvosk.so"
    vosk_library_works "$library_extract_tmp" \
      || die "the verified Vosk library could not load or lacks its required API"
    mv -fT -- "$library_extract_tmp" "$library_generation/libvosk.so" \
      || die "could not publish the verified Vosk library"
    library_extract_tmp=""
    rm -f -- "$library_archive"
    library_archive=""
    write_library_provenance "$library_generation"
  fi
  library_generation_works "$library_generation" \
    || die "the Vosk library generation failed exact payload/provenance validation"

  model_generation="$models_root/$model_generation_name"
  if [ -e "$model_generation" ] || [ -L "$model_generation" ]; then
    if [ ! -d "$model_generation" ] || [ -L "$model_generation" ]; then
      die "refusing unsafe Vosk model directory: $model_generation"
    fi
  fi
  if ! model_generation_works "$model_generation" \
      || ! vosk_model_works "$library_generation/libvosk.so" "$model_generation"; then
    archive="$models_root/.$model_generation_name.zip"
    fetch_verified "$KILIX_VOICE_MODEL_URL" "$archive" \
      "$KILIX_VOICE_MODEL_SHA256" "the $model_id speech model"
    staging="$(mktemp -d "$models_root/.extract.XXXXXX")" \
      || die "could not allocate model staging"
    unzip -q -d "$staging" -- "$archive" \
      || { rm -rf -- "$staging" "$archive"; die "could not extract the speech model"; }
    [ -d "$staging/$model_archive_directory" ] \
      || { rm -rf -- "$staging" "$archive"
           die "the speech model archive did not contain $model_archive_directory"; }
    model_payload_works "$staging/$model_archive_directory" \
      || { rm -rf -- "$staging" "$archive"
           die "the speech model archive is missing required model files"; }
    rm -rf -- "$model_generation"
    mv -- "$staging/$model_archive_directory" "$model_generation" \
      || { rm -rf -- "$staging" "$archive"; die "could not publish the speech model"; }
    # The extracted model is the artifact worth keeping; the archive is
    # re-fetchable and checksum-pinned, so it is not worth doubling the footprint.
    rm -rf -- "$staging" "$archive"
    write_model_provenance "$model_generation"
  fi
  model_generation_works "$model_generation" \
    || die "the Vosk model generation failed exact payload/provenance validation"
  vosk_model_works "$library_generation/libvosk.so" "$model_generation" \
    || die "the verified Vosk model could not be loaded by the pinned library"
fi

stamp_tmp="$(mktemp "$state_dir/.kilix-voice-refs.XXXXXX")" \
  || die "could not create install stamp"
# A read-aloud-only repair of the runtime must not downgrade the record of a
# full install: the library and model lines describe state this run did not
# touch.
final_refs="$expected_refs"
if full_stamp_satisfies_runtime; then
  final_refs="$(cat -- "$stamp")"
fi
printf '%s\n' "$final_refs" >"$stamp_tmp"
chmod 0600 "$stamp_tmp"

capture_previous_asset_links
transaction_active=1
if [ "$without_dictation" = 0 ]; then
  promote "$library_generation_name" "$library_root/current"
  library_link_changed=1
  # Catalog id -> immutable archive generation. Switching pins/tiers is one
  # relink and the generation it replaced remains available for rollback.
  promote "$model_generation_name" "$model"
  model_link_changed=1
fi
prepare_runtime_entrypoints
promote "generations/${uncommitted_generation##*/}" "$runtime_current"
runtime_changed=1
voice_runtime_works \
  || die "the published voice runtime generation did not pass validation"
mv -fT -- "$stamp_tmp" "$stamp" || die "could not publish the voice install stamp"
stamp_tmp=""
transaction_committed=1
if [ "$without_dictation" = 1 ]; then
  installed_dictation_model=""
  if dictation_assets_present; then
    log "the Vosk library and the $installed_dictation_model model are already installed; dictation stays available"
  else
    log "installed read-aloud only; rerun without --without-dictation to add the pinned Vosk library and model"
  fi
fi
log "installed and verified $prefix_bin/kilix-tts, $prefix_bin/kilix-stt, $prefix_bin/kilix-voiced"
