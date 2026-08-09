# Codex RISC-V GitHub release pipeline

## Implemented baseline

- Distribution repository shape: thin manifest + ordered patch series + release
  tooling; the upstream Git history is reconstructed for each run.
- Initial upstream: `rust-v0.147.0`, tag object
  `3ed6f04f6bf8b7c46299d1cb1ff99c74ce21a51d`, commit
  `be6e8eac029b183056b7e4402879f15d2c85f61b`.
- Stable target: `riscv64gc-unknown-linux-musl`, with `rv64gc` as the CPU
  baseline and Highway RVV dispatch disabled.
- Toolchain baseline: Rust 1.95.0, Zig 0.14.0, Bazel from the upstream
  `.bazelversion`, and V8 150.4.0 from the upstream lockfile.

## Patch ownership

The current downstream commits were exported as mail patches and tested
against the exact stable tag. OpenAI-owned release workflow changes,
DotSlash/R2 publication, npm trusted publishing, signing environments, and
private runner names are deliberately not included. The distribution
workflows own the RISC-V build and publication sequence.

## Runtime gate

GitHub builds candidates without K3 credentials. A local maintainer command
downloads and verifies one candidate run, tests the exact bytes under a remote
temporary directory on K3, removes the temporary files, and emits structured
JSON. Publication consumes that report and requires an explicit protected
environment approval.

## Failure classification

- Patch apply or target-list drift: downstream compatibility failure.
- V8 source-list or archive mismatch: V8 artifact failure.
- Rust/LLVM compiler SIGSEGV: toolchain code-generation failure.
- Archive/ELF/sidecar mismatch: packaging failure.
- Native command or kernel feature failure: K3 runtime failure.

The recorded ThinLTO/RVV compiler crash concerns the experimental RVA23/GNU
optimized CLI. It must not be used to weaken or skip the validated RV64GC musl
release path.

## Implementation verification (2026-08-09)

- The filtered patch series digest is
  `86e47e0ad2c1b3d55ecb5aa33c13af7e68349e35d5b774d012f607ef8e2a018b`.
- Exact reconstruction from the local upstream mirror verified tag object
  `3ed6f04f6bf8b7c46299d1cb1ff99c74ce21a51d`, peeled commit
  `be6e8eac029b183056b7e4402879f15d2c85f61b`, and applied all four mail
  patches without conflicts or whitespace errors.
- Two independent reconstructions produced the same patched HEAD,
  `70311605cdfdfb51c68f30c7ea3c4bdc151e1747`; `git am` uses each patch's
  author date as the committer date so identical inputs do not acquire a new
  commit SHA on every candidate run.
- Twelve upstream rusty-V8 helper tests, fourteen package-layout/Cargo helper
  tests, target-filtered Cargo locked metadata, and seventeen downstream
  release-policy/installer tests pass.
- The local Bazel 9 query could not start inside the restricted Codex sandbox:
  its JVM failed while enumerating/creating loopback networking. The same query
  remains an explicit compatibility workflow gate on an ordinary Ubuntu
  runner; this is an environment limitation, not a successful target query.
- No remote repository, GitHub App, protected environment, release, or K3
  mutation was created during local implementation.
- Read-only K3 preflight reports Linux 6.18.3 `riscv64`, Python 3.14.4, tar,
  sha256sum, and unshare. A direct unprivileged user/PID namespace probe
  succeeds. K3 currently has neither curl nor wget, which does not affect the
  candidate path because assets are copied over SSH and the installer is given
  local `--archive`/`--release-json` inputs.
- The live watcher resolved the same current stable tag, Rust 1.95.0, and V8
  150.4.0 without changing the manifest. All four workflows pass actionlint
  1.7.12 after checksum-verifying the validator binary.

## Review fixes (2026-08-09)

- Candidate run validation now accepts the Actions REST API `path` value
  `.github/workflows/candidate-build.yml`; the K3 command and publish workflow
  call one shared validator, which also tolerates an explicit `@ref` form.
- The bwrap namespace probe expects the supervised command at PID 2. Bubblewrap
  remains PID 1 inside its newly created PID namespace.
- Candidate construction starts only in an empty directory. Finalization and
  every later validation reject missing, extra, non-regular, or symlinked
  entries outside the sealed asset set plus `candidate.json`.
- Installer roots are resolved to absolute paths before constructing atomic
  symlinks. A regression test performs two installs with relative roots and
  checks both resolved targets.
- SPDX generation merges the target-filtered Codex workspace and ripgrep
  15.2.0 Cargo metadata. Ripgrep metadata is resolved with its `pcre2` feature,
  and Cargo graph edges are emitted as SPDX `DEPENDS_ON` relationships.

## First hosted build result and follow-up (2026-08-09)

- Compatibility run `31307029344` passed. Candidate run `31307066391` then
  built the complete RISC-V V8 target successfully (15,712 actions in
  9,104.438 seconds) before the first Cargo build rejected the stale lockfile.
- The pinned upstream release tree declares workspace version `0.147.0`, while
  135 workspace package entries in its checked-in `Cargo.lock` still recorded
  `0.0.0`. Re-resolving the same target changed only those workspace package
  versions; no registry package version or checksum changed.
- A fourth ordered mail patch carries that deterministic lockfile repair.
  Both compatibility and Candidate workflows now resolve the complete
  `riscv64gc-unknown-linux-musl` graph with `--locked` before the expensive V8
  build. The previous `--no-deps` compatibility check could not detect this
  workspace lock mismatch.
- The pinned Rust action is version-specific and accepts only target/component
  inputs. Its invalid `toolchain` input was removed, eliminating the hosted
  runner warning while retaining Rust 1.95.0 from the pinned action revision.

## Successful monolithic baseline and V8 split (2026-08-10)

- Candidate run `31313918744` succeeded from downstream commit
  `1e72a45e35962988d4c813f4398c05db5f963d8b`. The job completed in
  3 hours 34 minutes 33 seconds; candidate sealing, 16-subject build-provenance
  attestation, and artifact upload all passed.
- V8 remained the dominant cost: Bazel completed 15,712 actions in
  9,260.514 seconds (2 hours 34 minutes 20 seconds). The bwrap build took
  29.55 seconds, the main Codex release build took 53 minutes 20 seconds,
  ripgrep took 1 minute 21 seconds, and packaging/SBOM/upload consumed the
  remaining few minutes.
- The new `V8 build` workflow runs after Compatibility check and before
  Candidate build. It publishes an immutable prerelease keyed by the V8 crate
  version plus a content digest of the Bazel version/configuration, module
  lock, V8 helper scripts, `third_party/v8`, and the V8/LLVM/libc++ patch set.
  The key deliberately extracts only the V8 Cargo lock record, so unrelated
  Codex dependency changes do not force a V8 rebuild.
- Candidate build no longer sets up Bazel or invokes the V8 target. It derives
  the same key, downloads the exact four-file V8 release, validates its sealed
  input and asset maps, verifies the V8 workflow attestations, and passes the
  archive and binding through the existing `RUSTY_V8_ARCHIVE` and
  `RUSTY_V8_SRC_BINDING_PATH` interfaces.
- The V8 workflow uploads a small, exact-run handoff containing the original
  Compatibility source SHA, V8 tag, and full input digest. Candidate uses that
  handoff instead of trusting the nested workflow run's default-branch SHA,
  then recomputes and compares the identity after checking out the exact source.
- A one-time bootstrap path can consume a named successful legacy Candidate.
  It validates the complete candidate/run identity and verifies all three V8
  asset attestations before generating `v8-build.json` and re-attesting the
  four-file V8 release. A local rehearsal against run `31313918744` passed and
  produced input SHA-256
  `10f4df4a7b9da4e4e64160a0e013cac9a72f662dd65c74c643868d678ad6b4fe`
  and tag `rusty-v8-riscv64-v150.4.0-10f4df4a7b9d`.
