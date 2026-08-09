# Implementation review notes (2026-08-09)

## Verification performed

- Reconstructed `rust-v0.147.0` from the local upstream mirror and applied all
  three patches; the resulting commit is
  `5dbaa40f06186f07ce82bfeb58b0a326be72e316`.
- Downstream release-tool tests passed (9 tests).
- Focused upstream V8/package tests passed (12 + 14 tests), upstream installer
  tests passed (18 tests), and locked Cargo metadata without dependencies
  resolved successfully.
- On K3, the packaged bubblewrap starts the command as PID 2 with PID 1 as its
  parent (`pid=2 ppid=1 uid=1000`).

## Review defects retained for the inline report

- `validate_run()` expects an `@` suffix in the Actions REST `path`, although a
  normal workflow-run object reports `.github/workflows/candidate-build.yml`.
  The same predicate is repeated in the publish preflight.
- The required `bwrap-namespaces` smoke test requires the command itself to be
  PID 1, but bubblewrap remains PID 1 and launches the command as PID 2 on K3.
- Relative `--install-root`/`--bin-dir` values are embedded verbatim as symlink
  targets, so the installer reports success while both links resolve relative
  to the wrong parent directory.
- Candidate validation checks the metadata asset map but not unexpected files
  on disk; the upload and release globs therefore publish stale extra files that
  are absent from `release.json`, `SHA256SUMS`, and the K3 transfer.
- The SPDX input is only the Codex workspace metadata, so it omits the separately
  built and bundled ripgrep/PCRE2 graph (and other non-Cargo native components).

## Resolution

All five reported defects are fixed in the current checkout. Candidate-run
identity is checked by one shared validator, the K3 bwrap assertion reflects
the observed PID 1/2 layout, candidate directories are sealed as exact regular
file sets, installer roots are canonicalized before linking, and SPDX output
merges the target-filtered Codex and ripgrep Cargo dependency graphs.
