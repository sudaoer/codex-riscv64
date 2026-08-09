# Unofficial Codex builds for Linux/riscv64

This repository follows stable releases of
[`openai/codex`](https://github.com/openai/codex), applies a small RISC-V patch
series, cross-builds `riscv64gc-unknown-linux-musl` packages, and publishes the
exact candidate bytes that passed native validation.

This is an **unofficial downstream distribution**. It is not produced,
endorsed, signed, or supported by OpenAI.

## Release flow

```text
stable upstream release -> update PR -> compatibility checks
  -> immutable V8 resolve/build -> candidate build -> local K3 validation
  -> protected environment approval -> GitHub Release
```

The scheduled updater only follows stable `rust-vX.Y.Z` releases. Alpha builds
can be requested manually, but the publish preflight rejects them from the
stable channel.

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
# Validate the manifest and patch policy.
python3 scripts/release.py validate

# Reconstruct the downstream source from an exact upstream tag.
python3 scripts/release.py prepare \
  --source-dir .work/source \
  --upstream-url https://github.com/openai/codex.git

# Run release-tool tests.
python3 -m unittest discover -s tests -v

# Validate an Actions candidate on the configured SSH host and request publish.
python3 scripts/k3_validate.py --run-id RUN_ID --ssh-host k3 --request-publish
```

The release workflows, patch series, and manifest are the authoritative build
inputs. The existing full fork is only a patch-development workspace.

## One-time GitHub configuration

1. Create the public `sudaoer/codex-riscv64` repository and push this thin
   repository with `main` as the default branch.
2. Create a repository-scoped GitHub App with **Contents: read/write** and
   **Pull requests: read/write**. Install it only on this repository, store its
   app ID as the `UPDATER_APP_ID` Actions variable, and its private key as the
   `UPDATER_APP_PRIVATE_KEY` Actions secret.
3. Create a protected environment named `release`, add required reviewers, and
   restrict its deployment branch to `main`. The publish job cannot run until
   this environment is approved.
4. Protect `main`: require pull requests and the `Compatibility check / check`
   status. Keep the default workflow token read-only; individual jobs request
   only the extra permissions they need.

Do not add an SSH key for K3 to GitHub. Native validation is deliberately
initiated from the maintainer workstation, so an untrusted GitHub runner never
receives access to the machine.

## Maintenance model

The stable watcher runs daily and updates the exact annotated tag object,
peeled commit, Rust toolchain, and `rusty_v8` crate version in one fixed update
PR. Zig remains a downstream-controlled toolchain pin. A patch conflict or
focused upstream test failure blocks that PR instead of silently dropping a
patch.

After `main` passes compatibility checks, the V8 workflow derives a SHA-256
identity from the actual Bazel/V8/LLVM/libc++ inputs. It reuses the matching
attested prerelease when it exists, otherwise it builds and publishes that
immutable V8 pair once. Candidate build independently derives the same key,
downloads and verifies all four V8 release assets, then builds an immutable
14-day Actions artifact without rebuilding V8. A Codex update that does not
change those V8 inputs reuses the same release even if other Cargo lock entries
changed.

For the one-time migration from the old monolithic workflow, set the temporary
Actions variable `V8_BOOTSTRAP_CANDIDATE_RUN_ID` to a successful Candidate run.
The V8 workflow validates that candidate and its attestations before sealing
and re-attesting the extracted V8 bytes. Delete the variable after the first V8
release is created. It is ignored whenever the exact V8 release already exists.

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
the K3 evidence both before and after environment approval, creates a draft,
checks the remote asset set, and only then makes the release public. Existing
tags or releases are never overwritten.

## Published assets

Each release contains primary and app-server packages, the responses proxy,
the exact `rusty_v8` archive and binding, checksums, build metadata, SPDX SBOM,
K3 evidence, license notices, and the installer. GitHub artifact attestations
bind build outputs to the candidate workflow and source revision. Build
metadata also records the immutable V8 input digest, release tag, asset map,
and V8 builder identity.

Version 1 intentionally does not publish npm packages. RVA23/RVV builds remain
experimental and cannot pass the stable manifest policy.
