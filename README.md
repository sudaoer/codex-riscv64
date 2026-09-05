# 非官方 Codex Linux/riscv64 构建 | Unofficial Codex Builds for Linux/riscv64

## 这是什么？ / What is this?

这是面向 Linux/riscv64 的非官方 OpenAI Codex 下游发行版。项目跟随
[OpenAI Codex](https://github.com/openai/codex) 的稳定版本，提供可直接安装的
RISC-V 构建包。

This is an unofficial downstream distribution of OpenAI Codex for Linux/riscv64.
It follows stable versions of [OpenAI Codex](https://github.com/openai/codex) and
provides ready-to-install RISC-V packages.

本项目不是由 OpenAI 制作、背书、签名或支持的。请在使用前阅读
[最新 Release](https://github.com/sudaoer/codex-riscv64/releases/latest) 和
[安全策略](./SECURITY.md)。

This project is not produced, endorsed, signed, or supported by OpenAI. Before
installing, review the [latest Release](https://github.com/sudaoer/codex-riscv64/releases/latest)
and the [security policy](./SECURITY.md).

## 支持范围 / Support scope

当前稳定发布目标是 `riscv64gc-unknown-linux-musl`，CPU 基线为 RV64GC。也就是
说，主机应运行 Linux，并且 RISC-V 处理器应实现 RV64GC。

The current stable release target is `riscv64gc-unknown-linux-musl`, with an
RV64GC CPU baseline. The host must run Linux, and the RISC-V processor should
implement RV64GC.

稳定发布目前只覆盖以下范围：

- Linux/riscv64；
- `riscv64gc-unknown-linux-musl` 软件包；
- 通过 GitHub Release 分发的预编译版本。

Stable releases currently cover only:

- Linux/riscv64;
- the `riscv64gc-unknown-linux-musl` packages;
- prebuilt versions distributed through GitHub Releases.


## 快速安装 / Quick install

请先确认主机已经安装 `curl`、`python3`、`tar` 和 `sha256sum`。远程安装器会检查
操作系统和机器架构，然后下载 Release 元数据与主安装包，校验文件大小和 SHA-256，
再检查归档路径是否安全。

Make sure the host has `curl`, `python3`, `tar`, and `sha256sum`. The remote
installer checks the operating system and machine architecture, downloads the
Release metadata and primary package, verifies the size and SHA-256 digest, and
checks the archive paths for safety.

在 [Releases](https://github.com/sudaoer/codex-riscv64/releases) 页面已有可用版本时，
运行：

When a release is available on the [Releases](https://github.com/sudaoer/codex-riscv64/releases)
page, run:

```sh
curl -fsSL https://github.com/sudaoer/codex-riscv64/releases/latest/download/install.sh | sh
```

安装器默认会：

- 将版本安装到 `~/.codex/packages/standalone`；
- 在 `~/.local/bin/codex` 创建指向当前版本的链接；
- 保留已经安装的旧版本，便于回滚；
- 原子地切换 `current` 指向新版本。

By default, the installer will:

- install releases under `~/.codex/packages/standalone`;
- create `~/.local/bin/codex` as a link to the current version;
- keep previously installed versions for rollback;
- atomically switch `current` to the new version.

如果 `~/.local/bin` 尚未加入 `PATH`，可以先在当前 shell 中执行：

If `~/.local/bin` is not already on `PATH`, run this in the current shell first:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

要安装 Release 页面中的指定版本，可将 `TAG` 替换为实际的
`riscv-vX.Y.Z-rN` 标签：

To install a specific version from the Releases page, replace `TAG` with the
actual `riscv-vX.Y.Z-rN` tag:

```sh
curl -fsSL https://github.com/sudaoer/codex-riscv64/releases/latest/download/install.sh | sh -s -- --version TAG
```

## 首次运行 / First run

安装完成后，先确认版本并启动交互界面：

After installation, check the version and start the interactive interface:

```sh
codex --version
codex
```

也可以直接附带一个初始提示：

You can also provide an initial prompt directly:

```sh
codex "解释当前目录中的代码"
```

首次使用时可以选择 **Sign in with ChatGPT**。如果要使用 API key，请参阅
[Codex 认证文档](https://developers.openai.com/codex/auth)。登录方式和账户权限由
上游 Codex 决定，本项目不会替换 OpenAI 的认证流程。

On first use, choose **Sign in with ChatGPT**. To use an API key, see the
[Codex authentication documentation](https://developers.openai.com/codex/auth).
Authentication methods and account permissions are determined by upstream Codex;
this project does not replace OpenAI's authentication flow.

## 安装内容 / What's included

一键安装使用主包
`codex-package-riscv64gc-unknown-linux-musl.tar.gz`。主包至少包含：

The one-command installer uses the primary package
`codex-package-riscv64gc-unknown-linux-musl.tar.gz`. It includes at least:

- `bin/codex`：Codex CLI；
- `bin/codex-code-mode-host`：Code Mode 主机程序；
- `codex-resources/bwrap`：随包提供的 Linux sandbox helper；
- `codex-path/rg`：随包提供并启用 PCRE2 的 ripgrep。

- `bin/codex`: the Codex CLI;
- `bin/codex-code-mode-host`: the Code Mode host;
- `codex-resources/bwrap`: the bundled Linux sandbox helper;
- `codex-path/rg`: bundled ripgrep with PCRE2 enabled.

Release 页面还会单独提供 app-server 包和 Responses API proxy 包。它们适用于相应的
高级场景，不是普通 CLI 安装的必需品；请从 Release 页面下载与目标匹配的资产。

The Release page also provides separate app-server and Responses API proxy packages.
They are for their respective advanced use cases and are not required for a normal
CLI installation; download the asset matching your target from the Release page.

## 维护者验证 / Maintainer validation

每次实际生成 Candidate 后，Actions 会自动启动独立的 QEMU 验证工作流；9 项检查和
验证预检通过后，自动启动 Publish。Publish 会核对成功的 QEMU run/attempt、下载其报告，
并再次检查 Candidate、构建 attestation 和最新上游版本，再通过 `release` 环境发布。
`release` 环境保留分支限制，不再要求人工审批。报告和日志作为 Actions artifact 保留 14 天。
构建复用既有正式版本时跳过验证；`force_rebuild` 会强制构建并验证，但发布仍拒绝覆盖
既有 Release。维护者也可以单独重跑验证：

```sh
gh workflow run qemu-validate.yml --ref main -f candidate_run_id=RUN_ID
```

维护者可以用统一验证入口在 K3 主机或 QEMU riscv64 客体中检查 Candidate。默认目标仍是
K3；旧入口 `scripts/k3_validate.py` 保留兼容性：

```sh
python3 scripts/validate.py --target k3 --run-id RUN_ID
python3 scripts/k3_validate.py --run-id RUN_ID
python3 scripts/validate.py --target qemu --run-id RUN_ID
```

`--run-id` 是必需的 Candidate run ID；`--target` 可选且默认为 `k3`，`--policy` 默认为
`release/policy.toml`，`--output` 可指定报告路径。`--skip-attestation` 仅跳过本地验证中的
attestation 检查，`--request-publish` 请求发布工作流；Publish 始终独立检查 attestation。

QEMU 验证要求 Linux 主机上的以下工具和包：

```sh
sudo apt-get update
sudo apt-get install gh qemu-system-misc qemu-utils qemu-efi-riscv64 \
  cloud-image-utils genisoimage openssh-client
```

QEMU 默认使用 4 个 vCPU、4096 MiB 内存、900 秒启动超时和 10 倍测试超时倍率；原生 K3
默认倍率为 1。总验证时间上限为两小时。镜像固定为 Ubuntu Noble `release-20260826`：
`https://cloud-images.ubuntu.com/releases/releases/noble/release-20260826/ubuntu-24.04-server-cloudimg-riscv64.img`，
SHA-256 为
`6d0e58dc153585213020b0ec51112ebd70bedd5d2bc563599207f819586e141f`。
下载只会在校验通过后进入 `--qemu-cache-dir`（默认 `.work/qemu`）；已缓存镜像每次都会
重新校验，损坏缓存会重新下载。每轮验证都会创建 qcow2 overlay、UEFI 变量盘和 NoCloud
配置盘，结束或中断时清理临时客体资源。

检查以客体普通用户运行。初始化仅在临时客体内将
`kernel.apparmor_restrict_unprivileged_userns` 设为 `0`，让随包 bwrap 使用 user namespace，
并从宿主 UTC 初始化客体时钟；报告记录实际配置与时钟偏差。

可通过 `--qemu-cache-dir`、`--qemu-efi-dir`（默认 `/usr/share/qemu-efi-riscv64`）、
`--qemu-cpus`、`--qemu-memory-mib`、`--qemu-boot-timeout` 和 `--timeout-scale` 调整
QEMU 参数；`--ssh-host`（默认 `k3`）用于旧的原生入口。未指定 `--output` 时，成功报告
写入 `analysis/k3-report-riscv-vX.Y.Z-rN.json` 或
`analysis/qemu-report-riscv-vX.Y.Z-rN.json`，对应日志目录去掉 `.json` 后追加 `-logs`；失败
时会保留报告和日志，方便定位启动、SSH 或检查失败。

验证报告的正式发布资产名称仍为 `k3-report.json`；发布预检同时接受旧的 `--k3-report` 和
`--report` 参数。自动发布使用 `candidate_run_id`、`validation_run_id` 和
`validation_run_attempt` 定位成功验证的报告；有仓库写权限的人工维护者仍可使用
`k3_report_b64` 提交本地报告。这两种报告来源互斥，自动 bot 不能使用手工报告入口。
历史报告如果没有 `validation_target` 会按 `native-k3` 兼容处理；新报告会明确写入
`native-k3` 或 `qemu-system-riscv64`，发布说明也会显示实际目标。`--request-publish`
使用维护者的本地报告入口；Publish 预检通过后自动发布。

Whenever a new Candidate is produced, Actions automatically starts a separate QEMU
validation workflow. After all nine checks and validation preflight pass, it starts
Publish. Publish verifies the successful QEMU run and attempt, retrieves its report,
and independently checks the Candidate, build attestation, and latest upstream
version before publishing through the `release` environment. The environment retains
its branch restriction and does not require manual approval. Reports and logs are
retained as Actions artifacts for 14 days. Reusing an existing formal release skips
validation; `force_rebuild` builds and validates again, while publication still
refuses to overwrite an existing Release. Maintainers can also rerun validation:

```sh
gh workflow run qemu-validate.yml --ref main -f candidate_run_id=RUN_ID
```

Maintainers can use the single validation entry point against the K3 host or a QEMU
riscv64 guest. K3 remains the default, and the legacy `scripts/k3_validate.py` entry
point is kept for compatibility:

```sh
python3 scripts/validate.py --target k3 --run-id RUN_ID
python3 scripts/k3_validate.py --run-id RUN_ID
python3 scripts/validate.py --target qemu --run-id RUN_ID
```

`--run-id` is the required Candidate run ID. `--target` is optional and defaults to
`k3`; `--policy` defaults to `release/policy.toml`; and `--output` selects the report
path. `--skip-attestation` skips only the local attestation check, while
`--request-publish` requests the publication workflow. Publish always verifies the
build attestation independently.

QEMU validation requires these host packages on Ubuntu:

```sh
sudo apt-get update
sudo apt-get install gh qemu-system-misc qemu-utils qemu-efi-riscv64 \
  cloud-image-utils genisoimage openssh-client
```

The QEMU defaults are 4 vCPUs, 4096 MiB of memory, a 900-second boot timeout, and a
10x test timeout scale; native K3 uses a default scale of 1. The overall limit is two
hours. The guest image is pinned to
Ubuntu Noble `release-20260826` at
`https://cloud-images.ubuntu.com/releases/releases/noble/release-20260826/ubuntu-24.04-server-cloudimg-riscv64.img`
with SHA-256
`6d0e58dc153585213020b0ec51112ebd70bedd5d2bc563599207f819586e141f`.
Only verified downloads enter `--qemu-cache-dir` (default `.work/qemu`); cached bytes
are rechecked on every run and a corrupt cache is downloaded again. Each run creates a
qcow2 overlay, UEFI variable disk, and NoCloud seed, then removes temporary guest
resources on completion or interruption.

Checks run as an ordinary guest user. Initialization sets
`kernel.apparmor_restrict_unprivileged_userns=0` inside the temporary guest so the
bundled bwrap can use user namespaces, and initializes the guest clock from host
UTC. The report records the actual setting and clock offset.

Use `--qemu-cache-dir`, `--qemu-efi-dir` (default `/usr/share/qemu-efi-riscv64`),
`--qemu-cpus`, `--qemu-memory-mib`, `--qemu-boot-timeout`, and `--timeout-scale` to
adjust QEMU behavior. `--ssh-host` (default `k3`) selects the native host connection.
Without `--output`, a successful run writes
`analysis/k3-report-riscv-vX.Y.Z-rN.json` or
`analysis/qemu-report-riscv-vX.Y.Z-rN.json`; its logs use the report path with `.json`
replaced by `-logs`. Reports and logs are retained after failures for diagnosing boot,
SSH, or test failures.

The formal publication asset remains `k3-report.json`; publish preflight accepts both
the legacy `--k3-report` option and its `--report` alias. Automated publication uses
`candidate_run_id`, `validation_run_id`, and `validation_run_attempt` to retrieve the
successful validation report. Human maintainers with repository write access can
still submit local reports using `k3_report_b64`. The report inputs are mutually
exclusive, and automation bots cannot use the manual report path.
Historical reports without `validation_target` are accepted as
`native-k3`; new reports identify `native-k3` or `qemu-system-riscv64`, and release
notes display the actual target. `--request-publish` uses the maintainer's local
report path; publication proceeds automatically after Publish preflight succeeds.

已发布的稳定包会在 RISC-V Linux 主机或 QEMU riscv64 客体中进行验证，包括 CLI、sandbox、
Code Mode 通信、内置 `bwrap` 和 PCRE2 ripgrep 的基本检查。

Published stable packages are checked on a RISC-V Linux host or a QEMU riscv64 guest,
including basic checks of the CLI, sandbox, Code Mode communication, bundled `bwrap`,
and PCRE2 ripgrep.

## 限制与排查 / Limitations and troubleshooting

Code Mode 和 sandbox 都包含在主包中，但 sandbox 是否能正常运行仍取决于 Linux
内核是否允许所需的 user/PID namespace。如果启动 sandbox 时失败，请先检查：

Code Mode and the sandbox are included in the primary package, but sandbox
execution still depends on the Linux kernel allowing the required user and PID
namespaces. If sandbox startup fails, check:

```sh
uname -s
uname -m
command -v curl python3 tar sha256sum
```

在采用 Sv39 的内核上，V8 可能无法预留理想的 128 GiB sandbox 地址空间。Code Mode
仍然可用，但地址空间隔离强度会低于能够完成完整预留的主机；对隔离强度有严格要求
时，应把主机内核能力纳入评估。

On a host using an Sv39 kernel, V8 may be unable to reserve its ideal 128 GiB
sandbox address space. Code Mode remains available, but address-space isolation
is weaker than on a host that can complete the full reservation. If strong
isolation is required, include the host kernel capabilities in your evaluation.

如果 shell 找不到 `codex`，通常是 `~/.local/bin` 不在 `PATH` 中；如果安装器报告
架构错误，请确认 `uname -s` 为 `Linux` 且 `uname -m` 为 `riscv64`。如果最新 Release
暂时不可用，请先查看 [Releases 页面](https://github.com/sudaoer/codex-riscv64/releases)。

If the shell cannot find `codex`, `~/.local/bin` is usually missing from `PATH`. If
the installer reports an architecture error, confirm that `uname -s` is `Linux` and
`uname -m` is `riscv64`. If the latest Release is temporarily unavailable, check
the [Releases page](https://github.com/sudaoer/codex-riscv64/releases) first.

## 安全、来源与许可 / Security, provenance, and license

这是非官方下游构建，OpenAI 不对这些二进制文件提供背书或支持。当前只支持最新的
已发布下游版本；上游安全修复需要经过下游补丁、构建和原生验证后才会出现在这里。

These are unofficial downstream builds, and OpenAI does not endorse or support
these binaries. Only the latest published downstream release is supported here;
upstream security fixes appear only after downstream patching, building, and native
validation succeed.

安全问题请按照 [安全策略](./SECURITY.md) 私下报告，不要在公开 Issue 中披露可能影响
发布完整性、安装器或 sandbox 的问题。项目许可见 [LICENSE](./LICENSE)。

Report security issues privately according to the [security policy](./SECURITY.md)
rather than disclosing possible release-integrity, installer, or sandbox issues in
a public issue. See [LICENSE](./LICENSE) for the project license.
