"""Download GUI-Actor-3B weights from a GitHub Release mirror.

Usage:
    .venv/Scripts/python scripts/download_weights_from_github.py
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

import httpx

REPO = "LiaoZiqi-GZFLS/GUI-Actor-3B-Weights"
RELEASE_TAG = "v1.0.0"
MODEL_DIR = Path("models/gui-actor-3b")
CHUNK_SIZE = 8192 * 1024


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def list_release_assets(client: httpx.Client) -> list[dict]:
    url = f"https://api.github.com/repos/{REPO}/releases/tags/{RELEASE_TAG}"
    resp = client.get(url)
    resp.raise_for_status()
    return resp.json()["assets"]


def download(url: str, path: Path, client: httpx.Client) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with client.stream("GET", url, follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(path, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=CHUNK_SIZE):
                f.write(chunk)
    print(f"  Downloaded {path.name}")


def extract_7z_volumes(archive_stem: str, dir: Path) -> None:
    """Use 7z to extract the first volume of a split archive into dir."""
    seven_zip = Path("C:/Program Files/7-Zip/7z.exe")
    if not seven_zip.exists():
        from shutil import which
        exe = which("7z")
        if exe:
            seven_zip = Path(exe)
        else:
            raise RuntimeError("7-Zip not found")

    first_volume = dir / f"{archive_stem}.7z.001"
    if not first_volume.exists():
        raise FileNotFoundError(first_volume)

    # Extract to a temporary subdir, then move the resulting file up, so 7z's
    # default "preserve relative path" behaviour doesn't nest files under
    # models/gui-actor-3b/models/gui-actor-3b/.
    import subprocess
    extract_tmp = dir / ".extract_tmp"
    extract_tmp.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(seven_zip), "x", "-y", str(first_volume), f"-o{extract_tmp}"], check=True)

    extracted_files = list(extract_tmp.rglob("*"))
    files_to_move = [p for p in extracted_files if p.is_file()]
    for src in files_to_move:
        dst = dir / src.name
        if dst.exists():
            dst.unlink()
        src.rename(dst)
    shutil.rmtree(extract_tmp)
    print(f"  Extracted {archive_stem}")


def main() -> int:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    with httpx.Client(follow_redirects=True) as client:
        assets = list_release_assets(client)
        manifest_asset = next((a for a in assets if a["name"] == "sha256.txt"), None)
        if not manifest_asset:
            print("ERROR: sha256.txt not found in release assets", file=sys.stderr)
            return 1

        manifest_path = MODEL_DIR / "sha256.txt"
        download(manifest_asset["browser_download_url"], manifest_path, client)

        expected = {}
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            name, digest, _ = line.split("\t")
            expected[name] = digest

        # Download everything except the original safetensors entries (those are split).
        volume_groups: dict[str, list[str]] = {}
        optional_files = {".gitattributes"}  # GitHub release upload skips hidden files
        for name in expected:
            if name.endswith(".safetensors"):
                continue  # will be extracted from volumes
            m = re.match(r"^(.*\.safetensors)\.7z\.\d{3}$", name)
            if m:
                stem = m.group(1)
                volume_groups.setdefault(stem, []).append(name)
                continue
            # Small files copied as-is
            asset = next((a for a in assets if a["name"] == name), None)
            if not asset:
                if name in optional_files:
                    print(f"  Skipping optional file {name} (not in release assets)")
                    continue
                print(f"WARNING: asset {name} not found", file=sys.stderr)
                continue
            dst = MODEL_DIR / name
            if dst.exists() and sha256(dst) == expected[name]:
                print(f"  {name} already up to date")
            else:
                download(asset["browser_download_url"], dst, client)

        # Download and extract split archives
        for stem, volumes in volume_groups.items():
            target = MODEL_DIR / stem
            if target.exists() and sha256(target) == expected.get(stem, ""):
                print(f"  {stem} already up to date")
                continue

            for vol_name in sorted(volumes):
                asset = next((a for a in assets if a["name"] == vol_name), None)
                if not asset:
                    raise RuntimeError(f"Missing volume asset: {vol_name}")
                dst = MODEL_DIR / vol_name
                if dst.exists() and sha256(dst) == expected[vol_name]:
                    print(f"  {vol_name} already up to date")
                else:
                    download(asset["browser_download_url"], dst, client)

            extract_7z_volumes(stem, MODEL_DIR)
            # Clean up volume files after extraction
            for vol_name in volumes:
                (MODEL_DIR / vol_name).unlink(missing_ok=True)

    print("\nWeight download complete. Verify with:")
    print(f"  Get-FileHash {MODEL_DIR}/*.safetensors -Algorithm SHA256")
    return 0


if __name__ == "__main__":
    sys.exit(main())
