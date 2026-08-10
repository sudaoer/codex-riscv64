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

已发布的稳定包会在 RISC-V Linux 主机上进行原生验证，包括 CLI、sandbox、Code Mode
通信、内置 `bwrap` 和 PCRE2 ripgrep 的基本检查。

Published stable packages are natively checked on a RISC-V Linux host, including
basic checks of the CLI, sandbox, Code Mode communication, bundled `bwrap`, and
PCRE2 ripgrep.

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
