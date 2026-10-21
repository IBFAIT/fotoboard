"""Erzeugt Web-Versionen der Fotos für das Fotoboard.

Aufruf:  .venv/bin/python resize.py originale

Schreibt fotos/board/ (längste Seite 500 px) und fotos/gross/ (längste Seite
2000 px) als JPEG mit Qualität 85. Die EXIF-Drehung wird ins Bild übernommen,
kleinere Bilder werden nie vergrössert. Dateinamen bleiben unverändert, damit
Editor und Viewer die Fotos über den Namen zuordnen können.

EXIF-Daten (z. B. GPS-Position) werden nicht übernommen, das Farbprofil schon.
"""

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageOps

SIZES = {"board": 500, "gross": 2000}
QUALITY = 85
EXTENSIONS = {".jpg", ".jpeg"}


def save_version(img, icc, target, max_side):
    version = img.copy()
    # thumbnail() verkleinert nur und behält das Seitenverhältnis bei.
    version.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    options = {"quality": QUALITY, "optimize": True, "progressive": True}
    if icc:
        options["icc_profile"] = icc
    tmp = target.with_name(target.name + ".tmp")
    version.save(tmp, "JPEG", **options)
    tmp.replace(target)
    return version.size


def process(source, out_dirs, force):
    targets = {name: folder / source.name for name, folder in out_dirs.items()}
    if not force and all(
        t.exists() and t.stat().st_mtime >= source.stat().st_mtime
        for t in targets.values()
    ):
        return None

    with Image.open(source) as opened:
        icc = opened.info.get("icc_profile")
        img = ImageOps.exif_transpose(opened)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.load()

    return {
        name: save_version(img, icc, targets[name], SIZES[name]) for name in SIZES
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("quelle", type=Path, help="Ordner mit den Original-JPGs")
    parser.add_argument("--ziel", type=Path, default=Path("fotos"),
                        help="Zielordner (Standard: fotos)")
    parser.add_argument("--force", action="store_true",
                        help="auch Bilder neu erzeugen, die schon aktuell sind")
    args = parser.parse_args()

    if not args.quelle.is_dir():
        sys.exit(f"Ordner nicht gefunden: {args.quelle}")

    sources = sorted(
        p for p in args.quelle.iterdir()
        if p.is_file() and p.suffix.lower() in EXTENSIONS and not p.name.startswith(".")
    )
    if not sources:
        sys.exit(f"Keine JPGs in {args.quelle}")

    out_dirs = {name: args.ziel / name for name in SIZES}
    for folder in out_dirs.values():
        folder.mkdir(parents=True, exist_ok=True)

    done = skipped = failed = 0
    for i, source in enumerate(sources, 1):
        prefix = f"[{i:>3}/{len(sources)}] {source.name}"
        try:
            sizes = process(source, out_dirs, args.force)
        except Exception as exc:  # ein kaputtes Bild soll den Rest nicht stoppen
            failed += 1
            print(f"{prefix}  FEHLER: {exc}", file=sys.stderr)
            continue
        if sizes is None:
            skipped += 1
            print(f"{prefix}  aktuell, übersprungen")
        else:
            done += 1
            info = "  ".join(f"{n} {w}×{h}" for n, (w, h) in sizes.items())
            print(f"{prefix}  {info}")

    print(f"\nFertig: {done} erzeugt, {skipped} übersprungen, {failed} Fehler.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
