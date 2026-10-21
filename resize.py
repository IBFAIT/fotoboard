"""Erzeugt Web-Versionen der Fotos für das Fotoboard.

Aufruf:  .venv/bin/python resize.py originale

Schreibt drei Versionen, jeweils mit der längsten Seite in Pixeln:
  fotos/board/   500 px   fürs Board
  fotos/mittel/ 1000 px   wenn man ins Board hineinzoomt
  fotos/gross/  2000 px   für die Grossansicht

JPEG mit Qualität 85. Die EXIF-Drehung wird ins Bild übernommen, kleinere Bilder
werden nie vergrössert. Dateinamen bleiben unverändert, damit Editor und Viewer
die Fotos über den Namen zuordnen können. Erzeugt wird nur, was fehlt oder
älter ist als das Original.

EXIF-Daten (z. B. GPS-Position) werden nicht übernommen, das Farbprofil schon.
"""

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageOps

SIZES = {"board": 500, "mittel": 1000, "gross": 2000}
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


def process(source, out_dirs, force=False):
    """Erzeugt die fehlenden oder veralteten Versionen eines Fotos.

    Gibt {Version: (Breite, Höhe)} zurück, oder None, wenn alles aktuell war.
    """
    mtime = source.stat().st_mtime
    todo = {
        name: folder / source.name
        for name, folder in out_dirs.items()
        if force
        or not (folder / source.name).exists()
        or (folder / source.name).stat().st_mtime < mtime
    }
    if not todo:
        return None

    with Image.open(source) as opened:
        icc = opened.info.get("icc_profile")
        img = ImageOps.exif_transpose(opened)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.load()

    return {name: save_version(img, icc, target, SIZES[name]) for name, target in todo.items()}


def find_sources(folder):
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in EXTENSIONS and not p.name.startswith(".")
    )


def output_dirs(ziel):
    dirs = {name: Path(ziel) / name for name in SIZES}
    for folder in dirs.values():
        folder.mkdir(parents=True, exist_ok=True)
    return dirs


def resize_all(quelle, ziel, force=False, log=print, quiet=False):
    """Verarbeitet alle JPGs in quelle. Gibt (erzeugt, übersprungen, Fehler) zurück."""
    sources = find_sources(quelle)
    out_dirs = output_dirs(ziel)
    done = skipped = failed = 0
    for i, source in enumerate(sources, 1):
        prefix = f"[{i:>3}/{len(sources)}] {source.name}"
        try:
            sizes = process(source, out_dirs, force)
        except Exception as exc:  # ein kaputtes Bild soll den Rest nicht stoppen
            failed += 1
            log(f"{prefix}  FEHLER: {exc}")
            continue
        if sizes is None:
            skipped += 1
            if not quiet:
                log(f"{prefix}  aktuell, übersprungen")
        else:
            done += 1
            info = "  ".join(f"{n} {w}×{h}" for n, (w, h) in sizes.items())
            log(f"{prefix}  {info}")
    return done, skipped, failed


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
    if not find_sources(args.quelle):
        sys.exit(f"Keine JPGs in {args.quelle}")

    done, skipped, failed = resize_all(args.quelle, args.ziel, args.force)
    print(f"\nFertig: {done} erzeugt, {skipped} übersprungen, {failed} Fehler.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
