#!/usr/bin/env python3
"""ABS Renamer - rename audiobook folders/files based on matched Audiobookshelf metadata.

Safe by design:
  - Dry-run is the DEFAULT; --apply is required to actually rename
  - Every run writes a plan JSON (with inodes) usable by --undo
  - ABS re-links renamed items by inode (verified: LibraryScanner.js) so
    matches, covers and listening progress survive a plain `mv`
"""

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.local.json"
PLANS_DIR = SCRIPT_DIR / "plans"

DEFAULT_CONFIG = {
    "base_url": "https://audiobookshelf.mueller-nas.de/audiobookshelf",
    "api_key": "",
    "library_name": "Hörbücher",
    "container_root": "/audiobooks",
    "local_root": "/mnt/Mediathek/Hörbücher",
}

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

log = logging.getLogger("abs-renamer")


def load_config():
    if not CONFIG_FILE.exists():
        sys.exit(f"Config fehlt: {CONFIG_FILE}\n"
                 f"Beispiel: {json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2)}")
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = {**DEFAULT_CONFIG, **json.load(f)}
    if not cfg["api_key"]:
        sys.exit(f"api_key fehlt in {CONFIG_FILE}")
    return cfg


class AbsClient:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.api_key = api_key

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base}/api{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} bei {path}: {e.read().decode(errors='replace')[:200]}") from e

    def get_libraries(self) -> list:
        return self._request("GET", "/libraries").get("libraries", [])

    def get_library_items(self, lib_id: str) -> list:
        items, page = [], 0
        while True:
            res = self._request("GET", f"/libraries/{lib_id}/items?limit=100&page={page}&minified=1")
            items.extend(res.get("results", []))
            total = res.get("total", len(items))
            page += 1
            if len(items) >= total or not res.get("results"):
                break
        return items

    def get_item(self, item_id: str) -> dict:
        return self._request("GET", f"/items/{item_id}")

    def scan_library(self, lib_id: str) -> None:
        self._request("POST", f"/libraries/{lib_id}/scan", body={})


def sanitize(name: str, max_bytes: int = 240) -> str:
    name = INVALID_CHARS.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    while len(name.encode("utf-8")) > max_bytes and name:
        name = name[:-1]
    return name.strip()


def container_to_local(path: str, cfg: dict) -> Path | None:
    """Map a container-side item/file path to the host filesystem."""
    croot = cfg["container_root"].rstrip("/")
    lroot = Path(cfg["local_root"])
    if path and path.startswith(croot + "/"):
        return lroot / path[len(croot) + 1:]
    return None


def build_folder_name(meta: dict) -> str | None:
    authors = [a["name"] for a in meta.get("authors", []) if a.get("name")]
    title = meta.get("title") or ""
    series = meta.get("series") or []
    if not title or not authors:
        return None
    name = ", ".join(authors) + " - " + title
    if series:
        s = series[0]
        seq = s.get("sequence") or ""
        sname = s.get("name") or ""
        suffix = " ".join(x for x in [sname, seq] if x)
        if suffix:
            name += f" ({suffix})"
    return sanitize(name)


def plan_item(item: dict, cfg: dict) -> tuple[list[dict], list[dict], dict]:
    """Return (operations, skipped-notes, report-entry) for one item."""
    ops, notes = [], []
    item_id = item["id"]
    meta = (item.get("media") or {}).get("metadata") or {}
    asin = meta.get("asin")
    authors = meta.get("authors") or []

    entry = {"id": item_id, "path": item.get("path"), "current": item.get("relPath")}

    if not (asin or authors):
        entry["status"] = "skipped"
        entry["reason"] = "nicht gematcht (kein ASIN, keine Autoren) -> erst in ABS matchen"
        return ops, notes, entry

    folder_local = container_to_local(item.get("path", ""), cfg)
    if folder_local is None or not folder_local.is_dir():
        entry["status"] = "skipped"
        entry["reason"] = f"Ordner auf Host nicht gefunden: {item.get('path')}"
        return ops, notes, entry

    tracks = (item.get("media") or {}).get("tracks") or []
    title = meta.get("title") or folder_local.name

    # --- file renames ---
    file_ops = []
    if len(tracks) == 1:
        t = tracks[0]
        tpath = container_to_local((t.get("metadata") or {}).get("path", ""), cfg)
        if tpath and tpath.is_file():
            target_name = sanitize(title) + tpath.suffix
            if tpath.name != target_name:
                file_ops.append({"src": tpath, "dst_name": target_name, "ino": t.get("ino")})
    else:
        digits = max(2, len(str(len(tracks))))
        for t in tracks:
            tmeta = t.get("metadata") or {}
            tpath = container_to_local(tmeta.get("path", ""), cfg)
            if not tpath or not tpath.is_file():
                continue
            idx = t.get("index") or 0
            track_title = t.get("title") or tpath.stem
            target_name = f"{str(idx).zfill(digits)} - {sanitize(track_title)}{tpath.suffix}"
            if tpath.name != target_name:
                file_ops.append({"src": tpath, "dst_name": target_name, "ino": t.get("ino")})

    for op in file_ops:
        dst = op["src"].parent / op["dst_name"]
        if dst.exists():
            notes.append(f"Ziel existiert bereits, Datei übersprungen: {dst.name} (in {folder_local.name})")
        else:
            ops.append({"type": "file", "src": str(op["src"]), "dst": str(dst), "ino": op["ino"]})

    # --- folder rename ---
    new_folder = build_folder_name(meta)
    if new_folder and new_folder != folder_local.name:
        dst_folder = folder_local.parent / new_folder
        if dst_folder.exists():
            notes.append(f"Zielordner existiert bereits, Ordner übersprungen: {new_folder}")
        else:
            ops.append({"type": "folder", "src": str(folder_local), "dst": str(dst_folder),
                        "ino": item.get("ino")})

    if not ops:
        entry["status"] = "ok"
        entry["reason"] = "bereits korrekt benannt"
    else:
        entry["status"] = "planned"
        entry["operations"] = len(ops)
    return ops, notes, entry


def build_plan(client: AbsClient, cfg: dict, only_item: str | None) -> dict:
    libs = client.get_libraries()
    if not libs:
        raise RuntimeError("Keine Libraries in ABS gefunden")
    lib = None
    if only_item:
        one = client.get_item(only_item)
        lib_id = one["libraryId"]
        lib = next((l for l in libs if l["id"] == lib_id), {"id": lib_id, "name": lib_id})
        items = [one]
    else:
        lib = next((l for l in libs if cfg["library_name"].lower() in (l.get("name") or "").lower()), None)
        if lib is None:
            names = ", ".join(f"{l['name']} ({l['id']})" for l in libs)
            raise RuntimeError(f"Library '{cfg['library_name']}' nicht gefunden. Verfügbar: {names}")
        items = client.get_library_items(lib["id"])

    log.info(f"Library '{lib['name']}': {len(items)} Items geladen, Details werden abgerufen ...")

    operations, skipped, notes = [], [], []
    planned = 0
    for it in items:
        ops, it_notes, entry = plan_item(it, cfg)
        operations.extend(ops)
        notes.extend(it_notes)
        skipped.append(entry)
        if entry["status"] == "planned":
            planned += 1
        log.info(f"  [{entry['status']:>8}] {entry['current']}  ({entry['reason']})")
    for n in notes:
        log.warning(f"  HINWEIS: {n}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan = {
        "timestamp": ts,
        "library_id": lib["id"],
        "library_name": lib["name"],
        "base_url": cfg["base_url"],
        "local_root": cfg["local_root"],
        "item_filter": only_item,
        "counts": {"items": len(items), "planned": planned, "operations": len(operations)},
        "skipped": skipped,
        "operations": operations,
    }
    return plan


def save_plan(plan: dict) -> Path:
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    path = PLANS_DIR / f"plan_{plan['timestamp']}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    log.info(f"Plan gespeichert: {path}")
    return path


def apply_plan(plan: dict) -> None:
    files = [o for o in plan["operations"] if o["type"] == "file"]
    folders = [o for o in plan["operations"] if o["type"] == "folder"]
    done, failed = 0, 0
    for op in files + folders:  # files first, then folders
        try:
            os.rename(op["src"], op["dst"])
            done += 1
            log.info(f"  umbenannt: {op['src']} -> {op['dst']}")
        except OSError as e:
            failed += 1
            log.error(f"  FEHLER: {op['src']} -> {op['dst']}: {e}")
    log.info(f"Fertig: {done} umbenannt, {failed} fehlgeschlagen")
    if failed and done == 0:
        raise RuntimeError("Keine Operation erfolgreich - Scan wird nicht ausgelöst")


def undo_plan(plan: dict) -> None:
    files = [o for o in reversed(plan["operations"]) if o["type"] == "file"]
    folders = [o for o in reversed(plan["operations"]) if o["type"] == "folder"]
    done, failed = 0, 0
    for op in folders + files:  # folders back first, then files
        try:
            os.rename(op["dst"], op["src"])
            done += 1
            log.info(f"  zurück: {op['dst']} -> {op['src']}")
        except OSError as e:
            failed += 1
            log.error(f"  FEHLER: {op['dst']} -> {op['src']}: {e}")
    log.info(f"Undo fertig: {done} zurückbenannt, {failed} fehlgeschlagen")


def main():
    p = argparse.ArgumentParser(description="Rename audiobook folders/files via ABS metadata")
    p.add_argument("--apply", action="store_true", help="ausführen (Default: Dry-Run)")
    p.add_argument("--yes", action="store_true", help="Rückfrage bei --apply überspringen")
    p.add_argument("--item", metavar="ID", help="nur ein bestimmtes ABS-Item (Testlauf)")
    p.add_argument("--undo", metavar="PLAN.json", help="Plan-Datei rückgängig machen")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cfg = load_config()
    client = AbsClient(cfg["base_url"], cfg["api_key"])

    if args.undo:
        with open(args.undo, encoding="utf-8") as f:
            plan = json.load(f)
        if not args.yes:
            if input(f"{len(plan['operations'])} Operationen rückgängig machen? (yes/no): ").strip().lower() != "yes":
                sys.exit("Abgebrochen")
        undo_plan(plan)
        client.scan_library(plan["library_id"])
        log.info("Library-Scan ausgelöst")
        return

    plan = build_plan(client, cfg, args.item)
    save_plan(plan)

    print(f"\n=== {plan['counts']['operations']} Operationen für {plan['counts']['planned']} Items geplant ===")
    for op in plan["operations"]:
        print(f"  [{op['type']:>6}] {Path(op['src']).name}\n" + " " * 16 + f"-> {Path(op['dst']).name}")

    if not args.apply:
        print("\nDry-Run (nichts verändert). Mit --apply ausführen, --undo <plan.json> macht rückgängig.")
        return

    if not args.yes:
        if input(f"\n{plan['counts']['operations']} Umbenennungen jetzt ausführen? (yes/no): ").strip().lower() != "yes":
            sys.exit("Abgebrochen")
    apply_plan(plan)
    client.scan_library(plan["library_id"])
    log.info("Library-Scan ausgelöst - ABS verknüpft die Items per Inode neu")


if __name__ == "__main__":
    main()
