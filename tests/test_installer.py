from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.sh"
TARGET = "riscv64gc-unknown-linux-musl"
RELEASE_TAG = "riscv-v1.2.3-r1"


class InstallerTests(unittest.TestCase):
    def test_relative_install_paths_produce_valid_absolute_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            work.mkdir()
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            fake_uname = fake_bin / "uname"
            fake_uname.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  -s) printf 'Linux\\n' ;;\n"
                "  -m) printf 'riscv64\\n' ;;\n"
                "  *) exit 2 ;;\n"
                "esac\n"
            )
            fake_uname.chmod(0o755)

            package = root / "package"
            files = {
                "bin/codex": b"codex\n",
                "codex-package.json": b"{}\n",
                "codex-resources/bwrap": b"bwrap\n",
                "codex-path/rg": b"rg\n",
            }
            for name, content in files.items():
                path = package / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                if name != "codex-package.json":
                    path.chmod(0o755)

            archive = root / f"codex-package-{TARGET}.tar.gz"
            with tarfile.open(archive, "w:gz") as output:
                for path in sorted(package.rglob("*")):
                    output.add(path, arcname=path.relative_to(package))
            release_json = root / "release.json"
            release_json.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "release_tag": RELEASE_TAG,
                        "distribution": {"target": TARGET},
                        "assets": {
                            archive.name: {
                                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                                "size": archive.stat().st_size,
                            }
                        },
                    }
                )
            )
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            }
            command = [
                "sh",
                str(INSTALLER),
                "--archive",
                str(archive),
                "--release-json",
                str(release_json),
                "--install-root",
                "relative-root",
                "--bin-dir",
                "relative-bin",
            ]
            for _ in range(2):
                subprocess.run(command, cwd=work, env=environment, check=True)

            expected_release = (
                work / "relative-root" / "releases" / f"{RELEASE_TAG}-{TARGET}"
            ).resolve()
            current = work / "relative-root" / "current"
            codex = work / "relative-bin" / "codex"
            self.assertTrue(current.is_symlink())
            self.assertTrue(codex.is_symlink())
            self.assertEqual(current.resolve(), expected_release)
            self.assertEqual(codex.resolve(), expected_release / "bin" / "codex")
            self.assertTrue(Path(os.readlink(current)).is_absolute())
            self.assertTrue(Path(os.readlink(codex)).is_absolute())


if __name__ == "__main__":
    unittest.main()
