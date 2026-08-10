# Unofficial Codex builds for Linux/riscv64

This repository follows stable releases of
[`openai/codex`](https://github.com/openai/codex), applies a small RISC-V patch
series, cross-builds `riscv64gc-unknown-linux-musl` packages, and publishes the
exact candidate bytes that passed native validation.

This is an **unofficial downstream distribution**. It is not produced,
endorsed, signed, or supported by OpenAI.

## Release flow

```text
resolve latest stable -> immutable release lock -> compatibility checks
  -> immutable V8 resolve/build -> candidate build -> local K3 validation
  -> protected environment approval -> GitHub Release
```

The repository does not pin a Codex version. Every build chain resolves the
current stable `rust-vX.Y.Z` release once, records the annotated tag object,
peeled commit, Rust toolchain, and `rusty_v8` version in `release-lock.json`,
and carries that exact lock through every later stage. Alpha builds and
arbitrary commits are rejected from the stable channel.

## Install

After the first release is published:

```sh
curl -fsSL \
  https://github.com/sudaoer/codex-riscv64/releases/latest/download/install.sh \
  | sh
```

The installer uses the standalone layout under
`~/.codex/packages/standalone`, keeps old versions for rollback, atomically
updates `current`, and places `codex` in `~/.local/bin` by default.

## Maintainer commands

```sh
# Validate the version-free policy.
python3 scripts/release.py validate-policy

# Resolve the current stable upstream release without changing the repository.
python3 scripts/release.py resolve-latest --output /tmp/release-lock.json

# Validate the resolved release identity and patch policy.
python3 scripts/release.py \
  --release-lock /tmp/release-lock.json \
  validate

# Reconstruct the downstream source from an exact upstream tag.
python3 scripts/release.py \
  --release-lock /tmp/release-lock.json \
  prepare \
  --source-dir .work/source \
  --upstream-url https://github.com/openai/codex.git

# Run release-tool tests.
python3 -m unittest discover -s tests -v

# Validate an Actions candidate on the configured SSH host and request publish.
python3 scripts/k3_validate.py --run-id RUN_ID --ssh-host k3 --request-publish
```

The release workflows, policy, generated release lock, and patch series are the
authoritative build inputs. The existing full fork is only a patch-development
workspace.

## One-time GitHub configuration

1. Create the public `sudaoer/codex-riscv64` repository and push this thin
   repository with `main` as the default branch.
2. Create a protected environment named `release`, add required reviewers, and
   restrict its deployment branch to `main`. The publish job cannot run until
   this environment is approved.
3. Protect `main`: require pull requests and the `Compatibility check / check`
   status. Keep the default workflow token read-only; individual jobs request
   only the extra permissions they need.

Do not add an SSH key for K3 to GitHub. Native validation is deliberately
initiated from the maintainer workstation, so an untrusted GitHub runner never
receives access to the machine.

## Maintenance model

The stable watcher runs daily, resolves the latest stable upstream identity,
and checks for its downstream release tag. When that release is missing it
dispatches the compatibility/build chain with the repository workflow token;
it never writes a version file or update branch. Zig remains a
downstream-controlled toolchain pin. A patch conflict, lock normalization
failure, or focused upstream test failure blocks the build instead of silently
dropping a patch.

`distribution.revision` is the downstream rebuild counter. Leave it at `1`
for the first build of each upstream version and increment it only when a new
downstream release of the same upstream version is required.

Release tags sometimes carry workspace package versions in `Cargo.toml` while
their checked-in `Cargo.lock` still contains the development placeholder
`0.0.0`. Source preparation handles this without a version-specific patch: it
updates only source-less workspace members that declare
`version.workspace = true`, commits that normalization deterministically, and
rejects every other unexpected version transition.

After `main` passes compatibility checks, the V8 workflow derives a SHA-256
identity from the actual Bazel/V8/LLVM/libc++ inputs. It reuses the matching
attested prerelease when it exists, otherwise it builds and publishes that
immutable V8 pair once. Candidate build independently derives the same key,
downloads and verifies all four V8 release assets, then builds an immutable
14-day Actions artifact without rebuilding V8. A Codex update that does not
change those V8 inputs reuses the same release even if other Cargo lock entries
changed.

On the maintainer workstation:

```sh
python3 scripts/k3_validate.py --run-id RUN_ID --ssh-host k3
```

Inspect the JSON under `analysis/`, then request the protected publish step:

```sh
python3 scripts/k3_validate.py \
  --run-id RUN_ID \
  --ssh-host k3 \
  --request-publish
```

Publish downloads the selected candidate by run ID, revalidates every byte and
the K3 evidence both before and after environment approval, and verifies that
the carried lock is still the current upstream stable release. It then creates
a draft, checks the remote asset set, and only then makes the release public.
Existing tags or releases are never overwritten.

## Published assets

Each release contains primary and app-server packages, the responses proxy,
the exact `rusty_v8` archive and binding, checksums, build metadata, SPDX SBOM,
K3 evidence, license notices, and the installer. GitHub artifact attestations
bind build outputs to the candidate workflow and source revision. Build
metadata also records the immutable V8 input digest, release tag, asset map,
V8 builder identity, and resolved `release-lock.json`.

Version 1 intentionally does not publish npm packages. RVA23/RVV builds remain
experimental and cannot pass the stable policy.
