#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: build_candidate.sh --source-dir DIR --candidate-dir DIR --source-info FILE

Builds and seals the stable riscv64gc-unknown-linux-musl release candidate.
The upstream musl setup step must already have exported its GITHUB_ENV values.
EOF
}

source_dir=""
candidate_dir=""
source_info=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-dir)
      source_dir="${2:?--source-dir requires a value}"
      shift 2
      ;;
    --candidate-dir)
      candidate_dir="${2:?--candidate-dir requires a value}"
      shift 2
      ;;
    --source-info)
      source_info="${2:?--source-info requires a value}"
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

if [[ -z "$source_dir" || -z "$candidate_dir" || -z "$source_info" ]]; then
  usage >&2
  exit 2
fi
: "${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
: "${CARGO_HOME:?CARGO_HOME is required}"
: "${STRIP:?STRIP is required; run install-musl-build-tools.sh first}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_dir="$(cd "$source_dir" && pwd)"
source_info="$(cd "$(dirname "$source_info")" && pwd)/$(basename "$source_info")"
mkdir -p "$candidate_dir"
candidate_dir="$(cd "$candidate_dir" && pwd)"
if [[ -n "$(find "$candidate_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo "Candidate directory must be empty: $candidate_dir" >&2
  exit 1
fi
target="riscv64gc-unknown-linux-musl"
target_suffix="${target//-/_}"
release_dir="$source_dir/codex-rs/target/$target/release"
v8_archive="$candidate_dir/librusty_v8_ptrcomp_sandbox_release_${target}.a.gz"
v8_binding="$candidate_dir/src_binding_ptrcomp_sandbox_release_${target}.rs"
v8_checksums="$candidate_dir/rusty_v8_ptrcomp_sandbox_release_${target}.sha256"

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
  --output-dir "$candidate_dir" \
  --bazel-config v8-target-riscv64 \
  --sandbox
(cd "$candidate_dir" && sha256sum -c "$(basename "$v8_checksums")")
export RUSTY_V8_ARCHIVE="$v8_archive"
export RUSTY_V8_SRC_BINDING_PATH="$v8_binding"
export AWS_LC_SYS_NO_JITTER_ENTROPY=1

cd "$source_dir/codex-rs"
cargo build --locked --target "$target" --release --bin bwrap
"$STRIP" --strip-debug --strip-unneeded "$release_dir/bwrap"
export CODEX_BWRAP_SHA256
CODEX_BWRAP_SHA256="$(sha256sum "$release_dir/bwrap" | awk '{print $1}')"
cargo build --locked --target "$target" --release \
  --bin codex \
  --bin codex-code-mode-host \
  --bin codex-responses-api-proxy \
  --bin codex-app-server
for binary in codex codex-code-mode-host codex-responses-api-proxy codex-app-server; do
  "$STRIP" --strip-debug --strip-unneeded "$release_dir/$binary"
done

rg_root="$RUNNER_TEMP/ripgrep-$target"
cargo install ripgrep \
  --version 15.2.0 \
  --locked \
  --features pcre2 \
  --target "$target" \
  --root "$rg_root"
"$STRIP" --strip-debug --strip-unneeded "$rg_root/bin/rg"

for bundle in primary app-server; do
  GITHUB_WORKSPACE="$source_dir" RUNNER_TEMP="$RUNNER_TEMP" \
    bash "$source_dir/.github/scripts/build-codex-package-archive.sh" \
      --target "$target" \
      --bundle "$bundle" \
      --entrypoint-dir "$release_dir" \
      --archive-dir "$candidate_dir" \
      --rg-bin "$rg_root/bin/rg"
done
tar -C "$release_dir" -czf \
  "$candidate_dir/codex-responses-api-proxy-${target}.tar.gz" \
  codex-responses-api-proxy

cp "$repo_root/scripts/install.sh" "$candidate_dir/install.sh"
cp "$repo_root/LICENSE" "$candidate_dir/LICENSE"
cp "$repo_root/NOTICE" "$candidate_dir/NOTICE"
cargo metadata \
  --locked \
  --format-version 1 \
  --filter-platform "$target" \
  >"$RUNNER_TEMP/codex-cargo-metadata.json"
mapfile -t ripgrep_manifests < <(
  find "$CARGO_HOME/registry/src" \
    -type f \
    -path '*/ripgrep-15.2.0/Cargo.toml' \
    -print
)
if [[ "${#ripgrep_manifests[@]}" -ne 1 ]]; then
  echo "Expected one cached ripgrep 15.2.0 manifest, got ${#ripgrep_manifests[@]}" >&2
  printf '  %s\n' "${ripgrep_manifests[@]}" >&2
  exit 1
fi
cargo metadata \
  --locked \
  --format-version 1 \
  --filter-platform "$target" \
  --features pcre2 \
  --manifest-path "${ripgrep_manifests[0]}" \
  >"$RUNNER_TEMP/ripgrep-cargo-metadata.json"
python3 "$repo_root/scripts/release.py" sbom \
  --cargo-metadata "$RUNNER_TEMP/codex-cargo-metadata.json" \
  --cargo-metadata "$RUNNER_TEMP/ripgrep-cargo-metadata.json" \
  --namespace-seed "$GITHUB_RUN_ID:$GITHUB_SHA" \
  --output "$candidate_dir/sbom.spdx.json"
python3 "$repo_root/scripts/release.py" build-info \
  --source-info "$source_info" \
  --source-dir "$source_dir" \
  --output "$candidate_dir/build-info.json"
python3 "$repo_root/scripts/release.py" finalize-candidate \
  --candidate-dir "$candidate_dir" \
  --source-info "$source_info" \
  --run-id "$GITHUB_RUN_ID" \
  --head-sha "$GITHUB_SHA"
python3 "$repo_root/scripts/release.py" validate-candidate \
  --candidate-dir "$candidate_dir" >/dev/null
