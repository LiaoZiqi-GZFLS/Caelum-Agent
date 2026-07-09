"""Script to package GUI-Actor-3B weights for GitHub Releases.

Usage:
    .venv/Scripts/python scripts/package_weights_for_github.py

Requires 7-Zip at C:\\Program Files\\7-Zip\\7z.exe (or in PATH).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

MODEL_DIR = Path("models/gui-actor-3b")
PACKAGE_DIR = Path("dist/weights")
SEVEN_ZIP = Path("C:/Program Files/7-Zip/7z.exe")
VOLUME_SIZE = "1900m"


def find_7z() -> Path:
    if SEVEN_ZIP.exists():
        return SEVEN_ZIP
    from shutil import which
    exe = which("7z")
    if exe:
        return Path(exe)
    raise RuntimeError("7-Zip (7z.exe) not found. Please install 7-Zip.")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def package_file(z7: Path, src: Path, dst_dir: Path) -> list[Path]:
    """Create 1.9GB volumes for a single large file, return list of volume paths."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    archive_base = dst_dir / f"{src.name}.7z"
    cmd = [
        str(z7), "a", "-v" + VOLUME_SIZE, "-mx=0", str(archive_base), str(src)
    ]
    print(f"Packaging {src.name} ...")
    subprocess.run(cmd, check=True)

    volumes = sorted(dst_dir.glob(f"{src.name}.7z.*"))
    print(f"  Created {len(volumes)} volume(s)")
    return volumes


def main() -> int:
    if not MODEL_DIR.exists():
        print(f"ERROR: {MODEL_DIR} does not exist. Download weights first.", file=sys.stderr)
        return 1

    z7 = find_7z()
    print(f"Using 7-Zip: {z7}")

    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    manifest_lines = []

    # Find large files that exceed GitHub's 2 GB single-file limit.
    # Skip dotfiles (e.g. .gitattributes) because gh release upload ignores them.
    large_files = [
        p for p in MODEL_DIR.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.stat().st_size > 1_900_000_000
    ]
    small_files = [
        p for p in MODEL_DIR.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.stat().st_size <= 1_900_000_000
    ]

    for src in large_files:
        volumes = package_file(z7, src, PACKAGE_DIR)
        for vol in volumes:
            manifest_lines.append(f"{vol.name}\t{sha256(vol)}\t{vol.stat().st_size}")
        manifest_lines.append(f"{src.name}\t{sha256(src)}\t{src.stat().st_size}")

    # Copy small files as-is and add checksums.
    for src in small_files:
        dst = PACKAGE_DIR / src.name
        print(f"Copying {src.name} ...")
        # shutil.copy2 would be fine, but streaming saves memory for safety.
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            while True:
                chunk = fsrc.read(8192 * 1024)
                if not chunk:
                    break
                fdst.write(chunk)
        manifest_lines.append(f"{dst.name}\t{sha256(dst)}\t{dst.stat().st_size}")

    manifest_path = PACKAGE_DIR / "sha256.txt"
    manifest_path.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    print(f"Manifest written to {manifest_path}")
    print(f"\nNext step: gh release create v1.0.0 --title ... && gh release upload v1.0.0 {PACKAGE_DIR}/*")
    return 0


if __name__ == "__main__":
    sys.exit(main())
