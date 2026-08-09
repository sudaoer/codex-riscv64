#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: build_v8.sh --source-dir DIR --v8-dir DIR

Builds and seals the reusable rusty_v8 release pair for Linux/riscv64 musl.
EOF
}

source_dir=""
v8_dir=""
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

if [[ -z "$source_dir" || -z "$v8_dir" ]]; then
  usage >&2
  exit 2
fi
: "${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_dir="$(cd "$source_dir" && pwd)"
mkdir -p "$v8_dir"
v8_dir="$(cd "$v8_dir" && pwd)"
if [[ -n "$(find "$v8_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo "V8 directory must be empty: $v8_dir" >&2
  exit 1
fi

target="riscv64gc-unknown-linux-musl"
target_suffix="${target//-/_}"
cd "$source_dir"
python3 .github/scripts/run_bazel_with_buildbuddy.py \
  --noexperimental_remote_repo_contents_cache \
  build -c opt \
  --platforms=@llvm//platforms:linux_riscv64_musl \
  --config=rusty-v8-upstream-libcxx \
  --config=v8-target-riscv64 \
  "//third_party/v8:rusty_v8_sandbox_release_pair_${target_suffix}" \
  "--build_metadata=COMMIT_SHA=$(git rev-parse HEAD)"
python3 .github/scripts/rusty_v8_bazel.py stage-release-pair \
  --platform linux_riscv64_musl \
  --target "$target" \
  --compilation-mode opt \
  --output-dir "$v8_dir" \
  --bazel-config v8-target-riscv64 \
  --sandbox

python3 "$repo_root/scripts/release.py" finalize-v8 \
  --source-dir "$source_dir" \
  --v8-dir "$v8_dir" \
  --run-id "$GITHUB_RUN_ID" \
  --head-sha "$GITHUB_SHA" \
  --source-kind build
python3 "$repo_root/scripts/release.py" validate-v8 \
  --source-dir "$source_dir" \
  --v8-dir "$v8_dir" >/dev/null
