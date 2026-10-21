"""Fotoboard: Editor starten und Boards veröffentlichen.

  .venv/bin/python fotoboard.py                    Editor im Browser öffnen
  .venv/bin/python fotoboard.py veroeffentlichen   alle Boards auf GitHub stellen
  .venv/bin/python fotoboard.py vorschau           nur die Vorschaubilder erzeugen

Der Editor läuft nur auf diesem Computer (127.0.0.1). Er speichert die Boards
direkt in diesen Ordner: das Hauptboard als index.html, jedes weitere Board als
<adresse>/index.html. Neue Fotos legt er in originale/ ab und erzeugt ihre
Web-Versionen in fotos/.

Beim Veröffentlichen kommen nur Fotos ins Repository, die auf einem Board sind.
Die .gitignore pflegt das Skript dabei selbst.
"""

import argparse
import errno
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

sys.dont_write_bytecode = True  # kein __pycache__ im Projektordner
import resize  # noqa: E402

ROOT = Path(__file__).resolve().parent
ORIGINALE = ROOT / "originale"
FOTOS = ROOT / "fotos"
PORT = 8765
PREVIEW = "vorschau.jpg"
PREVIEW_SIZE = (1200, 630)
RESERVED = {"fotos", "originale", "api", "lib"}
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$")
DATA_RE = re.compile(r'<script id="fotoboard-data" type="application/json">(.*?)</script>', re.S)
OLD_DATA_RE = re.compile(r"const LAYOUT = (\{.*?\});\s*$", re.S | re.M)
TOOLS = [
    "editor.html", "resize.py", "fotoboard.py", "Fotoboard.command", "Veröffentlichen.command", ".nojekyll",
    "lib/photoswipe/photoswipe.umd.min.js", "lib/photoswipe/photoswipe.css", "lib/photoswipe/LICENSE.txt",
]
IGNORE_START = "# >>> fotoboard"
IGNORE_END = "# <<< fotoboard"
IGNORE_INTRO = [
    "# Fotos, die auf keinem Board sind, bleiben privat.",
    "# Den Block darunter pflegt fotoboard.py beim Veröffentlichen selbst.",
]
LEGACY_IGNORE = {
    "# Nicht auf dem Board, daher nicht veröffentlichen.",
    "# Kommt ein Foto zurück aufs Board, die Zeile hier löschen.",
}
MAX_UPLOAD = 200 * 1024 * 1024

WRITE_LOCK = threading.RLock()
PUBLISH_LOCK = threading.Lock()


class Problem(Exception):
    """Ein Fehler mit einer Meldung, die man so anzeigen kann."""


# ---------- Boards lesen ----------

def board_dir(slug):
    return ROOT / slug if slug else ROOT


def board_rel(slug, name="index.html"):
    return f"{slug}/{name}" if slug else name


def valid_slug(slug):
    return slug == "" or (SLUG_RE.match(slug) is not None and slug not in RESERVED)


def parse_board(text):
    """Holt die eingebetteten Board-Daten aus einer index.html (neues und altes Format)."""
    match = DATA_RE.search(text) or OLD_DATA_RE.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    data["items"] = [
        it for it in data["items"]
        if isinstance(it, dict) and isinstance(it.get("name"), str) and it["name"]
    ]
    return data


def scan_boards():
    """Alle Boards im Ordner: index.html und <adresse>/index.html."""
    candidates = [("", ROOT / "index.html")]
    for folder in sorted(p for p in ROOT.iterdir() if p.is_dir()):
        if valid_slug(folder.name) and folder.name:
            candidates.append((folder.name, folder / "index.html"))
    boards = []
    for slug, path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        data = parse_board(text)
        if data is not None:
            boards.append({"slug": slug, "html": text, "data": data})
    return boards


def board_title(board):
    return board["data"].get("title") or ("Fotoboard" if not board["slug"] else board["slug"])


def used_names(boards):
    return {it["name"] for b in boards for it in b["data"]["items"]}


def photo_names():
    folder = FOTOS / "board"
    if not folder.is_dir():
        return []
    return sorted(
        p.name for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in resize.EXTENSIONS and not p.name.startswith(".")
    )


def photo_list():
    out = []
    for name in photo_names():
        try:
            v = int((FOTOS / "board" / name).stat().st_mtime)
        except OSError:
            continue
        out.append({"name": name, "v": v})
    return out


# ---------- Vorschaubild zum Teilen ----------

_TILE_CACHE = {}


def _load_tile(name, side):
    from PIL import Image, ImageCms, ImageOps

    path = FOTOS / "board" / name
    try:
        key = (name, side, path.stat().st_mtime)
    except OSError:
        return Image.new("RGB", (side, side), (17, 17, 17))
    if key in _TILE_CACHE:
        return _TILE_CACHE[key]
    try:
        with Image.open(path) as opened:
            icc = opened.info.get("icc_profile")
            img = opened.convert("RGB")
        if icc:  # in sRGB umrechnen, damit die Farben in der Vorschau stimmen
            try:
                src = ImageCms.ImageCmsProfile(BytesIO(icc))
                img = ImageCms.profileToProfile(img, src, ImageCms.createProfile("sRGB"), outputMode="RGB")
            except Exception:
                pass
        tile = ImageOps.fit(img, (side, side), Image.Resampling.LANCZOS)
    except Exception:
        tile = Image.new("RGB", (side, side), (17, 17, 17))
    _TILE_CACHE[key] = tile
    return tile


def render_preview(data, target):
    """Zeichnet das ganze Board auf ein 1200×630-Bild, wie der Viewer es zeigt."""
    import math

    from PIL import Image

    width, height = PREVIEW_SIZE
    size = float(data.get("size") or 300)
    items = sorted(data["items"], key=lambda it: float(it.get("z") or 0))
    canvas = Image.new("RGB", (width, height), (0, 0, 0))

    def num(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 0.0
        return v if math.isfinite(v) else 0.0

    if items:
        boxes = []
        for it in items:
            r = math.radians(num(it.get("rot")))
            h = size / 2 * (abs(math.cos(r)) + abs(math.sin(r)))
            cx, cy = num(it.get("x")) + size / 2, num(it.get("y")) + size / 2
            boxes.append((cx - h, cy - h, cx + h, cy + h))
        min_x = min(b[0] for b in boxes)
        min_y = min(b[1] for b in boxes)
        bw = max(b[2] for b in boxes) - min_x
        bh = max(b[3] for b in boxes) - min_y
        pad = 48
        scale = min((width - 2 * pad) / bw, (height - 2 * pad) / bh, 500 / size)
        off_x = (width - bw * scale) / 2 - min_x * scale
        off_y = (height - bh * scale) / 2 - min_y * scale
        side = max(1, round(size * scale))
        for it in items:
            tile = _load_tile(it["name"], side)
            rot = num(it.get("rot")) % 360
            mask = None
            if rot:
                # CSS dreht im Uhrzeigersinn, Pillow dagegen
                tile = tile.convert("RGBA").rotate(-rot, resample=Image.Resampling.BICUBIC, expand=True)
                mask = tile
            cx = (num(it.get("x")) + size / 2) * scale + off_x
            cy = (num(it.get("y")) + size / 2) * scale + off_y
            canvas.paste(tile, (round(cx - tile.width / 2), round(cy - tile.height / 2)), mask)

    tmp = target.with_name(target.name + ".tmp")
    canvas.save(tmp, "JPEG", quality=85, optimize=True, progressive=True)
    tmp.replace(target)


def render_all_previews(boards, log=None):
    for b in boards:
        with WRITE_LOCK:
            render_preview(b["data"], board_dir(b["slug"]) / PREVIEW)
        if log:
            log(f"  {board_rel(b['slug'], PREVIEW)}")


class PreviewQueue:
    """Erzeugt das Vorschaubild kurz nach dem letzten Speichern eines Boards."""

    def __init__(self, delay=1.5):
        self.delay = delay
        self.timers = {}
        self.lock = threading.Lock()

    def schedule(self, slug):
        with self.lock:
            old = self.timers.pop(slug, None)
            if old:
                old.cancel()
            timer = threading.Timer(self.delay, self._run, args=(slug,))
            timer.daemon = True
            self.timers[slug] = timer
            timer.start()

    def _run(self, slug):
        with self.lock:
            self.timers.pop(slug, None)
        path = board_dir(slug) / "index.html"
        try:
            data = parse_board(path.read_text(encoding="utf-8"))
            if data is not None:
                with WRITE_LOCK:
                    render_preview(data, board_dir(slug) / PREVIEW)
        except Exception as exc:
            print(f"Vorschaubild für {board_rel(slug)} ging nicht: {exc}", file=sys.stderr)


# ---------- Fotos ----------

RESIZE_LOCK = threading.Lock()


def resize_new(log=print):
    if not ORIGINALE.is_dir():
        return 0, 0, 0
    with RESIZE_LOCK:
        return resize.resize_all(ORIGINALE, FOTOS, log=log, quiet=True)


def save_upload(name, body):
    """Legt ein neues Original ab und erzeugt seine Web-Versionen."""
    if (
        Path(name).name != name
        or name.startswith(".")
        or Path(name).suffix.lower() not in resize.EXTENSIONS
        or any(ord(c) < 32 for c in name)
    ):
        raise Problem(f"{name}: Es gehen nur JPG-Dateien (.jpg oder .jpeg).")
    ORIGINALE.mkdir(exist_ok=True)
    target = ORIGINALE / name
    if target.exists():
        if target.read_bytes() != body:
            raise Problem(f"In originale/ liegt schon ein anderes Foto namens {name}. Benenne die Datei um.")
    else:
        tmp = target.with_name("." + name + ".tmp")
        tmp.write_bytes(body)
        tmp.replace(target)
    with RESIZE_LOCK:
        try:
            resize.process(target, resize.output_dirs(FOTOS))
        except Exception as exc:
            raise Problem(f"{name} liess sich nicht verarbeiten: {exc}") from exc
    return int((FOTOS / "board" / name).stat().st_mtime)


# ---------- Speichern aus dem Editor ----------

def write_board(slug, html_text):
    if not valid_slug(slug):
        raise Problem(f"Ungültige Adresse: {slug!r}")
    if len(html_text) > 10_000_000:
        raise Problem("Die Seite ist zu gross.")
    if parse_board(html_text) is None:
        raise Problem("Das ist keine Fotoboard-Seite.")
    folder = board_dir(slug)
    with WRITE_LOCK:
        folder.mkdir(exist_ok=True)
        target = folder / "index.html"
        tmp = folder / ".index.html.tmp"
        tmp.write_text(html_text, encoding="utf-8")
        tmp.replace(target)


def delete_board(slug):
    if not slug or not valid_slug(slug):
        raise Problem("Das Hauptboard kann man nicht löschen.")
    folder = board_dir(slug)
    with WRITE_LOCK:
        for name in ("index.html", PREVIEW):
            try:
                (folder / name).unlink()
            except FileNotFoundError:
                pass
        try:
            folder.rmdir()  # nur wenn sonst nichts drin liegt
        except OSError:
            pass


# ---------- Git und Veröffentlichen ----------

def git(*args, check=True):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        result = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, env=env)
    except FileNotFoundError as exc:
        raise Problem("git ist nicht installiert.") from exc
    if check and result.returncode:
        msg = (result.stderr or result.stdout).strip()
        raise Problem(f"git {args[0]} ging nicht: {msg}")
    return result


def ensure_repo():
    if git("rev-parse", "--is-inside-work-tree", check=False).returncode:
        raise Problem("Dieser Ordner ist kein Git-Repository.")
    if git("remote", "get-url", "origin", check=False).returncode:
        raise Problem("Es ist kein GitHub-Repository verbunden (git remote «origin» fehlt).")


def site_url():
    """Öffentliche Adresse der Seite, z. B. https://name.github.io/fotoboard/"""
    env = os.environ.get("FOTOBOARD_ADRESSE", "").strip()
    if env:
        return env.rstrip("/") + "/"
    cname = ROOT / "CNAME"
    if cname.is_file():
        host = cname.read_text(encoding="utf-8").strip()
        if host:
            return f"https://{host}/"
    result = git("remote", "get-url", "origin", check=False)
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", result.stdout.strip())
    if not match:
        return ""
    owner, repo = match.group(1).lower(), match.group(2)
    if repo.lower() == f"{owner}.github.io":
        return f"https://{owner}.github.io/"
    return f"https://{owner}.github.io/{repo}/"


def tracked_files():
    out = git("ls-files", "-z").stdout
    return {p for p in out.split("\0") if p}


def has_upstream():
    return git("rev-parse", "--abbrev-ref", "@{u}", check=False).returncode == 0


def unpushed_commits():
    if has_upstream():
        return int(git("rev-list", "--count", "@{u}..HEAD").stdout.strip() or 0)
    result = git("rev-list", "--count", "HEAD", check=False)
    return int(result.stdout.strip() or 0) if result.returncode == 0 else 0


def changed(rel, tracked):
    if rel not in tracked:
        return (ROOT / rel).exists()
    return git("diff", "--quiet", "HEAD", "--", rel, check=False).returncode != 0


def plan():
    """Was beim Veröffentlichen passieren würde, ohne etwas zu verändern."""
    ensure_repo()
    boards = scan_boards()
    if not boards:
        raise Problem("Kein Board gefunden. Lege im Editor zuerst ein Board an.")
    site = site_url()
    tracked = tracked_files()
    used = used_names(boards)
    available = set(photo_names())
    missing = sorted(n for n in used if n not in available and not (ORIGINALE / n).is_file())

    board_info = []
    for b in boards:
        rel = board_rel(b["slug"])
        status = "neu" if rel not in tracked else ("geändert" if changed(rel, tracked) else "unverändert")
        board_info.append({
            "slug": b["slug"],
            "title": board_title(b),
            "count": len(b["data"]["items"]),
            "status": status,
            "url": site + (b["slug"] + "/" if b["slug"] else ""),
        })

    present = {board_rel(b["slug"]) for b in boards}
    removed_boards = sorted(
        p.rsplit("/", 1)[0] for p in tracked
        if p.count("/") == 1 and p.endswith("/index.html") and p not in present
        and not p.startswith(("fotos/", "originale/"))
    )
    tracked_photos = {p.split("/")[-1] for p in tracked if p.startswith("fotos/")}
    new_photos = sorted(n for n in used if f"fotos/board/{n}" not in tracked)
    # Fotos, die schon online sind, aber eine neue Grösse bekommen haben
    new_versions = sorted(
        n for n in used if n not in new_photos and any(
            f"fotos/{v}/{n}" not in tracked and (FOTOS / v / n).is_file() for v in resize.SIZES
        )
    )
    removed_photos = sorted(n for n in tracked_photos if n not in used)
    tools = [t for t in TOOLS if (ROOT / t).exists() and changed(t, tracked)]
    ahead = unpushed_commits() if has_upstream() else 0
    nothing = (
        not new_photos and not new_versions and not removed_photos and not removed_boards
        and not tools and not ahead
        and all(b["status"] == "unverändert" for b in board_info)
    )
    return {
        "site": site,
        "boards": board_info,
        "removed_boards": removed_boards,
        "new_photos": new_photos,
        "new_versions": new_versions,
        "removed_photos": removed_photos,
        "missing": missing,
        "tools": tools,
        "ahead": ahead,
        "nothing": nothing,
    }


def update_gitignore(unused):
    path = ROOT / ".gitignore"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    keep, inside = [], False
    for line in lines:
        if line.strip() == IGNORE_START:
            inside = True
            continue
        if inside:
            if line.strip() == IGNORE_END:
                inside = False
            continue
        if line in LEGACY_IGNORE or line in IGNORE_INTRO or re.fullmatch(r"fotos/\*/[^/]+", line):
            continue
        keep.append(line)
    while keep and not keep[-1].strip():
        keep.pop()
    block = IGNORE_INTRO + [IGNORE_START] + [f"fotos/*/{n}" for n in sorted(unused)] + [IGNORE_END]
    text = "\n".join(keep + [""] + block) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


def commit_message(boards, info):
    lines = ["Fotoboard veröffentlicht", ""]
    for b in info["boards"]:
        if b["status"] != "unverändert":
            lines.append(f"- {b['title']}: {b['count']} Fotos ({b['status']})")
    for slug in info["removed_boards"]:
        lines.append(f"- Board {slug}/ entfernt")
    def fotos(n):
        return "1 Foto" if n == 1 else f"{n} Fotos"

    if info["new_photos"]:
        lines.append(f"- {fotos(len(info['new_photos']))} dazu")
    if info["new_versions"]:
        lines.append(f"- grössere Versionen für {fotos(len(info['new_versions']))}")
    if info["removed_photos"]:
        lines.append(f"- {fotos(len(info['removed_photos']))} nicht mehr online")
    if info["tools"]:
        lines.append(f"- Werkzeuge: {', '.join(info['tools'])}")
    if len(lines) == 2:
        lines.append("- Vorschaubilder aktualisiert")
    return "\n".join(lines) + "\n"


def publish(log=print):
    """Fotos und Vorschaubilder aktualisieren, committen und pushen."""
    if not PUBLISH_LOCK.acquire(blocking=False):
        raise Problem("Es läuft schon eine Veröffentlichung.")
    try:
        ensure_repo()
        log("Web-Versionen der Fotos prüfen …")
        done, _, failed = resize_new(log=log)
        if failed:
            raise Problem(f"{failed} Fotos liessen sich nicht verarbeiten, siehe oben.")
        if done:
            log(f"  {done} Fotos neu verkleinert")

        boards = scan_boards()
        if not boards:
            raise Problem("Kein Board gefunden. Lege im Editor zuerst ein Board an.")
        used = used_names(boards)
        available = set(photo_names())
        missing = sorted(used - available)
        if missing:
            raise Problem(
                "Diese Fotos sind auf einem Board, fehlen aber in originale/: " + ", ".join(missing)
            )

        log("Vorschaubilder erzeugen …")
        render_all_previews(boards, log)

        info = plan()
        unused = set(available) - used
        update_gitignore(unused)

        tracked = tracked_files()
        untrack = sorted(p for p in tracked if p.startswith("fotos/") and p.split("/")[-1] in unused)
        if untrack:
            git("rm", "--cached", "-q", "--", *untrack)

        paths = [".gitignore"]
        for b in boards:
            paths += [board_rel(b["slug"]), board_rel(b["slug"], PREVIEW)]
        for name in sorted(used):
            for version in resize.SIZES:
                rel = f"fotos/{version}/{name}"
                if (ROOT / rel).exists():
                    paths.append(rel)
        paths += [t for t in TOOLS if (ROOT / t).exists()]
        git("add", "--", *paths)
        git("add", "-u", "--", ":/")  # gelöschte Boards und Fotos

        staged = git("diff", "--cached", "--quiet", check=False).returncode != 0
        if staged:
            log("Änderungen festhalten (commit) …")
            git("commit", "-q", "-m", commit_message(boards, info))
        elif not unpushed_commits():
            log("Alles ist schon online. Es gibt nichts Neues zu veröffentlichen.")
            return {"changed": False, "url": info["site"], "boards": info["boards"]}

        log("Hochladen zu GitHub (push) …")
        if has_upstream():
            git("push", "-q")
        else:
            git("push", "-q", "-u", "origin", "HEAD")
        log("Fertig. GitHub braucht jetzt etwa eine Minute, bis alles online ist.")
        for b in info["boards"]:
            if b["url"]:
                log(f"  {b['title']}: {b['url']}")
        return {"changed": True, "url": info["site"], "boards": info["boards"]}
    finally:
        PUBLISH_LOCK.release()


def print_plan(info):
    print("Diese Boards gehen online:")
    for b in info["boards"]:
        print(f"  {b['title']} ({b['count']} Fotos, {b['status']})  {b['url']}")
    for slug in info["removed_boards"]:
        print(f"  {slug}/ wird entfernt")
    if info["new_photos"]:
        print(f"Neue Fotos: {len(info['new_photos'])}")
    if info["new_versions"]:
        print(f"Grössere Versionen für {len(info['new_versions'])} Fotos")
    if info["removed_photos"]:
        print(f"Fotos, die nicht mehr online sind: {len(info['removed_photos'])}")
    if info["missing"]:
        print("Fehlende Fotos: " + ", ".join(info["missing"]))


# ---------- Lokaler Server für den Editor ----------

class Handler(SimpleHTTPRequestHandler):
    server_version = "Fotoboard"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    # Nur Anfragen an 127.0.0.1/localhost mit diesem Port, so können fremde
    # Webseiten den Editor-Server nicht über einen Umweg erreichen.
    def _host_ok(self):
        return self.headers.get("Host", "") in self.server.allowed_hosts

    def _api_ok(self):
        if not self._host_ok() or self.headers.get("X-Fotoboard") != "1":
            return False
        origin = self.headers.get("Origin")
        return not origin or urlsplit(origin).netloc in self.server.allowed_hosts

    def _blocked(self, path):
        parts = [p for p in unquote(path).split("/") if p]
        return (
            any(p.startswith(".") for p in parts)
            or (parts and parts[0] == "originale")
            or path.endswith((".py", ".command"))
        )

    def do_GET(self):
        self._serve(head=False)

    def do_HEAD(self):
        self._serve(head=True)

    def _serve(self, head):
        if not self._host_ok():
            return self.send_error(403)
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            if head or not self._api_ok():
                return self.send_error(403)
            return self._api_get(path)
        if self._blocked(path):
            return self.send_error(404)
        return super().do_HEAD() if head else super().do_GET()

    def do_POST(self):
        if not self._api_ok():
            return self.send_error(403)
        url = urlsplit(self.path)
        try:
            if url.path == "/api/save":
                body = self._json()
                slug = body.get("slug", "")
                write_board(slug, body.get("html", ""))
                self.server.previews.schedule(slug)
                return self._send({"ok": True})
            if url.path == "/api/delete":
                delete_board(self._json().get("slug", ""))
                return self._send({"ok": True})
            if url.path == "/api/upload":
                name = parse_qs(url.query).get("name", [""])[0]
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= MAX_UPLOAD:
                    raise Problem(f"{name}: Datei fehlt oder ist zu gross.")
                body = self.rfile.read(length)
                return self._send({"ok": True, "name": name, "v": save_upload(name, body)})
            if url.path == "/api/publish":
                lines = []
                try:
                    result = publish(log=lambda msg: (lines.append(msg), print(msg)))
                except Problem as exc:
                    print(f"Fehler: {exc}")
                    return self._send({"ok": False, "error": str(exc), "log": lines}, 400)
                return self._send({"ok": True, "log": lines, **result})
            return self.send_error(404)
        except Problem as exc:
            return self._send({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            return self._send({"ok": False, "error": f"Unerwarteter Fehler: {exc}"}, 500)

    def _api_get(self, path):
        try:
            if path == "/api/info":
                return self._send({
                    "ok": True,
                    "site": site_url(),
                    "boards": scan_boards(),
                    "photos": photo_list(),
                })
            if path == "/api/plan":
                return self._send({"ok": True, **plan()})
            return self.send_error(404)
        except Problem as exc:
            return self._send({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            return self._send({"ok": False, "error": f"Unerwarteter Fehler: {exc}"}, 500)

    def _json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 20_000_000:
            raise Problem("Leere oder zu grosse Anfrage.")
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError as exc:
            raise Problem("Ungültige Anfrage.") from exc
        if not isinstance(data, dict):
            raise Problem("Ungültige Anfrage.")
        return data

    def _send(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def running_editor(port):
    """True, wenn auf dem Port schon ein Fotoboard-Editor läuft."""
    import urllib.request

    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/info", headers={"X-Fotoboard": "1"})
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.load(resp).get("ok") is True
    except Exception:
        return False


def serve(port, open_browser=True):
    if not (ROOT / "editor.html").is_file():
        sys.exit("editor.html fehlt in diesem Ordner.")
    url = f"http://127.0.0.1:{port}/editor.html"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE and running_editor(port):
            print(f"Der Editor läuft schon: {url}")
            if open_browser:
                webbrowser.open(url)
            return
        sys.exit(f"Port {port} ist belegt. Starte mit --port und einer anderen Zahl.")
    server.daemon_threads = True
    server.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    server.previews = PreviewQueue()

    print("Fotoboard-Editor läuft:", url)
    print("Lass dieses Fenster offen, solange du arbeitest. Ctrl+C beendet den Editor.\n")

    def warmup():
        done, _, failed = resize_new(log=print)
        if done or failed:
            print(f"Web-Versionen: {done} neu erzeugt, {failed} Fehler.\n")

    threading.Thread(target=warmup, daemon=True).start()
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEditor beendet.")
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(
        description="Fotoboard-Editor starten oder veröffentlichen.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("befehl", nargs="?", default="editor",
                        choices=["editor", "veroeffentlichen", "vorschau"])
    parser.add_argument("--port", type=int, default=PORT, help=f"Port für den Editor (Standard {PORT})")
    parser.add_argument("--ohne-browser", action="store_true", help="Browser nicht automatisch öffnen")
    parser.add_argument("--ja", action="store_true", help="beim Veröffentlichen nicht nachfragen")
    args = parser.parse_args()

    if args.befehl == "editor":
        serve(args.port, open_browser=not args.ohne_browser)
        return
    try:
        if args.befehl == "vorschau":
            boards = scan_boards()
            render_all_previews(boards, log=print)
            print(f"{len(boards)} Vorschaubilder erzeugt.")
            return
        info = plan()
        print_plan(info)
        if info["nothing"]:
            print("\nAlles ist schon online.")
        if not args.ja:
            answer = input("\nJetzt veröffentlichen? [j/n] ").strip().lower()
            if answer not in ("j", "ja", "y", "yes"):
                print("Abgebrochen, nichts verändert.")
                return
        print()
        publish(log=print)
    except Problem as exc:
        sys.exit(f"Fehler: {exc}")
    except KeyboardInterrupt:
        sys.exit("\nAbgebrochen.")


if __name__ == "__main__":
    main()
