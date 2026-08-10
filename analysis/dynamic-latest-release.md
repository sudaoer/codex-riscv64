# Dynamic latest-stable release implementation

## Intent

The downstream repository no longer stores a Codex version. Its committed
configuration contains only the stable-channel, target, toolchain-policy, and
downstream revision choices. Each build chain resolves one published upstream
`rust-vX.Y.Z` release and freezes its tag object, peeled commit, Rust version,
and `rusty_v8` version in `release-lock.json`.

## Trust and data flow

- Compatibility creates the lock and reconstructs the exact upstream source.
- V8 consumes the Compatibility artifact and includes the lock plus its
  canonical digest in the Candidate handoff.
- Candidate independently reconstructs source, recomputes the V8 identity,
  and publishes the lock as a sealed candidate asset.
- K3 validation and protected publication load the lock from the candidate,
  validate it against the current policy, and require it to remain the latest
  stable upstream identity.
- The daily watcher only dispatches this chain when the matching downstream
  release is absent; it does not write branches or version files.

## Cargo release normalization

The old fourth patch embedded one release's workspace version in 135
`Cargo.lock` entries. It is replaced by a strict preparation-time normalizer:

1. Require the resolved upstream version to equal `workspace.package.version`.
2. Enumerate workspace members that inherit `version.workspace = true`.
3. Accept only matching source-less lock entries whose version is either the
   release version or the upstream development placeholder `0.0.0`.
4. Change only placeholder versions, reparse and verify the complete result,
   then create a deterministic source commit.

Any missing package, duplicate lock entry, sourced placeholder, unrelated
placeholder, or unexpected version aborts reconstruction.

## Release boundary

Stable output remains `riscv64gc-unknown-linux-musl` with an `rv64gc` baseline,
bundled PCRE2 ripgrep, sandboxed Code Mode V8, bwrap, the app server, and the
responses proxy. GNU, RVA23/RVV, npm publishing, R2, and OpenAI-owned signing
remain outside this repository's release scope.

## Local validation evidence

On 2026-08-10 the live resolver selected upstream `rust-v0.147.0`, peeled it
to its immutable commit, selected Rust 1.95.0 and V8 150.4.0, and produced a
canonical lock digest of
`0485258f10fc067713382a5c707730e431fca7f4c18e6fc7982309888658db32`.
The latest-release verifier independently accepted that lock.

Two clean reconstructions produced the same downstream source commit
`e6daff3145013deccbe24468a0e03bac786f3cf5`, the same normalized Cargo lock
digest `bc4fe4509bbf7c4d19f3c04a40c23dfe2f13c07d7204feb548ee0ba419c661f1`,
and the same count of 135 normalized workspace packages. The reconstructed
tree was clean after preparation. Its 12 focused `rusty_v8` helper tests and
14 package tests passed, and `cargo metadata --locked` accepted the
`riscv64gc-unknown-linux-musl` graph (1191 packages).

The downstream suite passed 26 unit tests, Python bytecode compilation, shell
syntax checks, `actionlint`, policy validation, and `git diff --check`.
