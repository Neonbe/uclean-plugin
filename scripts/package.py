#!/usr/bin/env python3
"""Build explicit allowlisted source packages. Never include machine scan records."""
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    version = json.loads((ROOT / "plugin.json").read_text())["version"]
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    roots = ["skills", "assets", "README.md", "PRIVACY.md", "TERMS.md", "LICENSE", "RELEASE.md"]
    common = []
    for name in roots:
        path = ROOT / name
        common.extend([p for p in path.rglob("*") if p.is_file()] if path.is_dir() else [path])
    common = [p for p in common if "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo") and p.name != ".DS_Store"]
    checksums = []
    for host, manifests in {"codex": ["plugin.json", ".codex-plugin/plugin.json"], "claude": [".claude-plugin/plugin.json"]}.items():
        output = dist / ("uclean-" + host + "-" + version + ".zip")
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(common + [ROOT / m for m in manifests]):
                relative = path.relative_to(ROOT).as_posix()
                info = zipfile.ZipInfo(relative, (2026, 10, 30, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
        checksums.append(hashlib.sha256(output.read_bytes()).hexdigest() + "  " + output.name)
        print(output)
    (dist / "SHA256SUMS").write_text("\n".join(checksums) + "\n")


if __name__ == "__main__":
    main()
