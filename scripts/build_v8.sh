#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: build_v8.sh --source-dir DIR --v8-dir DIR --release-lock FILE [--local]

Builds and seals the reusable rusty_v8 release pair for Linux/riscv64 musl.
EOF
}

source_dir=""
v8_dir=""
release_lock=""
local_build=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-dir)
      source_dir="${2:?--source-dir requires a value}"
      shift 2
      ;;
    --v8-dir)
      v8_dir="${2:?--v8-dir requires a value}"
      shift 2
      ;;
    --release-lock)
      release_lock="${2:?--release-lock requires a value}"
      shift 2
      ;;
    --local)
      local_build=true
      shift
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

if [[ -z "$source_dir" || -z "$v8_dir" || -z "$release_lock" ]]; then
  usage >&2
  exit 2
fi
validation_args=()
if [[ "$local_build" == true ]]; then
  : "${DOWNSTREAM_SOURCE_SHA:?需要下游源码提交 SHA}"
  builder_args=(--source-kind local --head-sha "$DOWNSTREAM_SOURCE_SHA")
  validation_args=(--local)
else
  : "${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"
  : "${GITHUB_SHA:?GITHUB_SHA is required}"
  builder_args=(
    --source-kind build --run-id "$GITHUB_RUN_ID"
    --head-sha "${DOWNSTREAM_SOURCE_SHA:-$GITHUB_SHA}" --workflow-head-sha "$GITHUB_SHA"
  )
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_dir="$(cd "$source_dir" && pwd)"
release_lock="$(cd "$(dirname "$release_lock")" && pwd)/$(basename "$release_lock")"
mkdir -p "$v8_dir"
v8_dir="$(cd "$v8_dir" && pwd)"
if [[ -n "$(find "$v8_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo "V8 directory must be empty: $v8_dir" >&2
  exit 1
fi

target="riscv64gc-unknown-linux-musl"
target_suffix="${target//-/_}"
cd "$source_dir"
bazel_runner="$(PYTHONPATH="$repo_root/scripts" python3 - <<'PY'
from pathlib import Path

from release_lib import v8_bazel_runner

print(v8_bazel_runner(Path.cwd()))
PY
)"
python3 "$bazel_runner" \
  --noexperimental_remote_repo_contents_cache \
  build -c opt \
  --platforms=@llvm//platforms:linux_riscv64_musl \
  "//third_party/v8:rusty_v8_sandbox_release_pair_${target_suffix}" \
  "--build_metadata=COMMIT_SHA=$(git rev-parse HEAD)"
python3 .github/scripts/rusty_v8_bazel.py stage-release-pair \
  --platform linux_riscv64_musl \
  --target "$target" \
  --compilation-mode opt \
  --output-dir "$v8_dir" \
  --sandbox

python3 "$repo_root/scripts/release.py" --release-lock "$release_lock" finalize-v8 \
  --source-dir "$source_dir" \
  --v8-dir "$v8_dir" \
  "${builder_args[@]}"
python3 "$repo_root/scripts/release.py" --release-lock "$release_lock" validate-v8 \
  --source-dir "$source_dir" \
  --v8-dir "$v8_dir" "${validation_args[@]}" >/dev/null
