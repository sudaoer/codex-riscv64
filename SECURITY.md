# Security policy

This repository publishes unofficial downstream binaries. Report a suspected
release-integrity, installer, workflow-permission, or bundled-sandbox issue
privately through GitHub Security Advisories rather than a public issue.

Only the latest published downstream release is supported. Upstream Codex
security fixes are consumed through the stable update workflow, but downstream
availability is not guaranteed until the patch, build, and validation gates all pass.
Validation may run on the native K3 host or inside the managed QEMU riscv64 guest;
the published report records the actual target.

Release provenance can be checked with GitHub CLI:

```sh
gh attestation verify codex-package-riscv64gc-unknown-linux-musl.tar.gz --repo sudaoer/codex-riscv64
sha256sum -c SHA256SUMS
```
