#!/bin/sh
set -eu

repository="${CODEX_RISCV64_REPOSITORY:-sudaoer/codex-riscv64}"
install_root="${CODEX_RISCV64_INSTALL_ROOT:-${CODEX_HOME:-${HOME}/.codex}/packages/standalone}"
bin_dir="${CODEX_RISCV64_BIN_DIR:-${HOME}/.local/bin}"
release_tag="latest"
archive_path=""
release_json_path=""
staging_dir=""

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  --version TAG          Install a specific riscv-vX.Y.Z-rN release.
  --install-root DIR     Standalone package root.
  --bin-dir DIR          Directory in which to place the codex symlink.
  --archive FILE         Use a local primary package archive.
  --release-json FILE    Use matching local release metadata.
  -h, --help             Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --version)
      release_tag="${2:?--version requires a value}"
      shift 2
      ;;
    --install-root)
      install_root="${2:?--install-root requires a value}"
      shift 2
      ;;
    --bin-dir)
      bin_dir="${2:?--bin-dir requires a value}"
      shift 2
      ;;
    --archive)
      archive_path="${2:?--archive requires a value}"
      shift 2
      ;;
    --release-json)
      release_json_path="${2:?--release-json requires a value}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unexpected argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ "$(uname -s)" != "Linux" ] || [ "$(uname -m)" != "riscv64" ]; then
  echo "This installer supports Linux/riscv64 only." >&2
  exit 1
fi
for command_name in python3 tar sha256sum; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command not found: $command_name" >&2
    exit 1
  fi
done
if [ -z "$install_root" ] || [ -z "$bin_dir" ]; then
  echo "Install root and bin directory must not be empty." >&2
  exit 2
fi
install_root="$(
  python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' \
    "$install_root"
)"
bin_dir="$(
  python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' \
    "$bin_dir"
)"
if [ -n "$archive_path" ] && [ -z "$release_json_path" ]; then
  echo "--archive requires --release-json." >&2
  exit 2
fi
if [ -z "$archive_path" ] && [ -n "$release_json_path" ]; then
  echo "--release-json requires --archive." >&2
  exit 2
fi

temporary_dir="$(mktemp -d "${TMPDIR:-/tmp}/codex-riscv64-install.XXXXXX")"
cleanup() {
  if [ -n "$staging_dir" ] && [ -d "$staging_dir" ]; then
    rm -rf "$staging_dir"
  fi
  rm -rf "$temporary_dir"
}
trap cleanup EXIT HUP INT TERM

if [ -z "$release_json_path" ]; then
  if ! command -v curl >/dev/null 2>&1; then
    echo "Required command not found for remote install: curl" >&2
    exit 1
  fi
  release_json_path="$temporary_dir/release.json"
  if [ "$release_tag" = "latest" ]; then
    metadata_url="https://github.com/${repository}/releases/latest/download/release.json"
  else
    metadata_url="https://github.com/${repository}/releases/download/${release_tag}/release.json"
  fi
  curl -fsSL "$metadata_url" -o "$release_json_path"
fi

metadata_fields="$temporary_dir/metadata-fields"
python3 - "$release_json_path" "$release_tag" >"$metadata_fields" <<'PY'
import json
import re
import sys
from pathlib import Path

metadata_path = Path(sys.argv[1])
requested_tag = sys.argv[2]
value = json.loads(metadata_path.read_text(encoding="utf-8"))
target = "riscv64gc-unknown-linux-musl"
tag = value.get("release_tag")
if value.get("schema_version") != 1:
    raise SystemExit("unsupported release metadata schema")
if not isinstance(tag, str) or re.fullmatch(r"riscv-v[0-9]+\.[0-9]+\.[0-9]+-r[1-9][0-9]*", tag) is None:
    raise SystemExit("invalid release tag in metadata")
if requested_tag != "latest" and requested_tag != tag:
    raise SystemExit("requested tag does not match release metadata")
distribution = value.get("distribution")
if not isinstance(distribution, dict) or distribution.get("target") != target:
    raise SystemExit("release metadata is not for the stable RISC-V target")
asset_name = f"codex-package-{target}.tar.gz"
record = value.get("assets", {}).get(asset_name)
if not isinstance(record, dict):
    raise SystemExit("primary package is absent from release metadata")
digest = record.get("sha256")
size = record.get("size")
if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
    raise SystemExit("primary package digest is invalid")
if not isinstance(size, int) or size <= 0:
    raise SystemExit("primary package size is invalid")
print(tag)
print(asset_name)
print(digest)
print(size)
PY

resolved_tag="$(sed -n '1p' "$metadata_fields")"
asset_name="$(sed -n '2p' "$metadata_fields")"
expected_sha256="$(sed -n '3p' "$metadata_fields")"
expected_size="$(sed -n '4p' "$metadata_fields")"

if [ -z "$archive_path" ]; then
  archive_path="$temporary_dir/$asset_name"
  archive_url="https://github.com/${repository}/releases/download/${resolved_tag}/${asset_name}"
  curl -fsSL "$archive_url" -o "$archive_path"
fi

actual_size="$(wc -c < "$archive_path" | tr -d ' ')"
if [ "$actual_size" != "$expected_size" ]; then
  echo "Archive size mismatch: expected $expected_size, got $actual_size" >&2
  exit 1
fi
printf '%s  %s\n' "$expected_sha256" "$archive_path" | sha256sum -c -

python3 - "$archive_path" <<'PY'
import sys
import tarfile
from pathlib import PurePosixPath

with tarfile.open(sys.argv[1], "r:gz") as archive:
    members = archive.getmembers()
    if not members:
        raise SystemExit("package archive is empty")
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"unsafe package archive path: {member.name}")
        if member.issym() or member.islnk() or member.isdev():
            raise SystemExit(f"unsafe package archive member: {member.name}")
    required = {"bin/codex", "codex-package.json", "codex-resources/bwrap", "codex-path/rg"}
    names = {member.name.rstrip("/") for member in members}
    missing = sorted(required - names)
    if missing:
        raise SystemExit(f"package archive is missing required files: {missing}")
PY

releases_dir="$install_root/releases"
release_dir="$releases_dir/${resolved_tag}-riscv64gc-unknown-linux-musl"
staging_dir="$releases_dir/.${resolved_tag}.tmp.$$"
mkdir -p "$releases_dir" "$bin_dir"
if [ ! -d "$release_dir" ]; then
  mkdir "$staging_dir"
  tar -xzf "$archive_path" -C "$staging_dir"
  mv "$staging_dir" "$release_dir"
fi

current_link="$install_root/current"
current_tmp="$install_root/.current.tmp.$$"
ln -s "$release_dir" "$current_tmp"
mv -fT "$current_tmp" "$current_link"

codex_link="$bin_dir/codex"
codex_tmp="$bin_dir/.codex.tmp.$$"
ln -s "$current_link/bin/codex" "$codex_tmp"
mv -fT "$codex_tmp" "$codex_link"

echo "Installed Codex ${resolved_tag} for Linux/riscv64."
echo "Binary: ${codex_link}"
