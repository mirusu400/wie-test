#!/usr/bin/env python3
"""wie compatibility tester - fetch corpus, run wie_cli per game, render report."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from tqdm import tqdm

ROOT = Path(__file__).resolve().parent
CORPUS = ROOT / "corpus"
RESULTS = ROOT / "results"
DOWNLOADS = ROOT / "downloads"

ARCHIVE_ITEM = "dubigame.tistory.com_mirror_202403"
ARCHIVE_FILE = "dubigame.tistory.com.zip"
ARCHIVE_URL = f"https://archive.org/download/{ARCHIVE_ITEM}/{ARCHIVE_FILE}"

GAME_EXTS = (".jar", ".zip", ".jad")

# stderr pattern → status
PATTERNS = [
    ("unsupported_format", re.compile(r"Unknown (archive|file) format", re.I)),
    (
        "unimplemented",
        re.compile(
            r"(not yet implemented|unimplemented|todo!|InvalidJavaMethod|MethodNotFound)",
            re.I,
        ),
    ),
    ("panic", re.compile(r"panicked at", re.I)),
    ("load_error", re.compile(r"(failed to (load|parse|read)|Error: )", re.I)),
]


# ---------- fetch ----------


def cmd_fetch(args: argparse.Namespace) -> int:
    DOWNLOADS.mkdir(exist_ok=True)
    CORPUS.mkdir(exist_ok=True)
    target = DOWNLOADS / ARCHIVE_FILE

    if not target.exists() or args.redownload:
        download_with_resume(ARCHIVE_URL, target)
    else:
        print(
            f"[fetch] already have {target.name} ({target.stat().st_size/1e6:.1f} MB)"
        )

    print("[fetch] extracting...")
    extract_recursive(target, CORPUS)
    print(f"[fetch] done. corpus at {CORPUS}")
    return 0


def download_with_resume(url: str, dest: Path) -> None:
    pos = dest.stat().st_size if dest.exists() else 0
    headers = {"Range": f"bytes={pos}-"} if pos else {}
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0)) + pos
        mode = "ab" if pos else "wb"
        with open(dest, mode) as f, tqdm(
            total=total, initial=pos, unit="B", unit_scale=True, desc=dest.name
        ) as bar:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                bar.update(len(chunk))


def extract_recursive(zip_path: Path, dest: Path) -> None:
    """Extract outer zip; do NOT recurse into inner zips (they may be game archives)."""
    with zipfile.ZipFile(zip_path) as z:
        members = [m for m in z.namelist() if not m.endswith("/")]
        for m in tqdm(members, desc="extract"):
            try:
                z.extract(m, dest)
            except Exception as e:
                print(f"  ! skip {m}: {e}", file=sys.stderr)


# ---------- test ----------


def cmd_test(args: argparse.Namespace) -> int:
    wie = Path(args.wie).resolve()
    if not wie.exists():
        print(f"wie_cli not found at {wie}", file=sys.stderr)
        return 2

    RESULTS.mkdir(exist_ok=True)
    games = list(discover_games(CORPUS))
    if args.limit:
        games = games[: args.limit]
    print(
        f"[test] {len(games)} candidate games (timeout={args.timeout}s, jobs={args.jobs})"
    )

    pending = [g for g in games if not result_path(g).exists() or args.force]
    print(f"[test] {len(pending)} to run ({len(games)-len(pending)} cached)")

    bar = tqdm(total=len(pending), desc="test")
    counts: Counter[str] = Counter()

    def work(game: Path) -> dict:
        return run_one(wie, game, args.timeout)

    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futures = {ex.submit(work, g): g for g in pending}
        for fut in as_completed(futures):
            game = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = make_result(game, "runner_error", str(e), "", -1, 0.0)
            write_result(game, res)
            counts[res["status"]] += 1
            bar.set_postfix(dict(counts.most_common(4)))
            bar.update(1)
    bar.close()

    print("[test] summary:")
    for status, n in counts.most_common():
        print(f"  {status:22s} {n}")
    return 0


def discover_games(root: Path):
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in GAME_EXTS:
            # skip companion .jar when we have .jad (wie_cli reads .jad and finds .jar itself)
            if p.suffix.lower() == ".jar" and p.with_suffix(".jad").exists():
                continue
            yield p


def game_id(path: Path) -> str:
    rel = str(path.relative_to(CORPUS)).replace("\\", "/")
    h = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12]
    return h


def result_path(game: Path) -> Path:
    return RESULTS / f"{game_id(game)}.json"


def run_one(wie: Path, game: Path, timeout: float) -> dict:
    start = time.monotonic()
    creationflags = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW = 0x08000000 — hides the child console (winit window still appears)
        creationflags = 0x08000000

    try:
        proc = subprocess.Popen(
            [str(wie), str(game)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
    except OSError as e:
        return make_result(game, "runner_error", f"spawn failed: {e}", "", -1, 0.0)

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        rc = proc.returncode
        elapsed = time.monotonic() - start
        stderr_s = safe_decode(stderr)
        status = classify(rc, stderr_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            stderr = b""
        elapsed = time.monotonic() - start
        stderr_s = safe_decode(stderr)
        # If stderr already shows a fatal pattern before timeout, classify as that.
        early = classify_stderr_only(stderr_s)
        status = early if early else "ok_alive"
        rc = -1

    return make_result(
        game, status, summarize_stderr(stderr_s), stderr_s[-6000:], rc, elapsed
    )


def safe_decode(b: bytes) -> str:
    for enc in ("utf-8", "cp949", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


def classify(rc: int, stderr: str) -> str:
    s = classify_stderr_only(stderr)
    if s:
        return s
    if rc == 0:
        return "ok_exit"
    return "load_error" if rc != 0 else "ok_exit"


def classify_stderr_only(stderr: str) -> str | None:
    for status, pat in PATTERNS:
        if pat.search(stderr):
            return status
    return None


def summarize_stderr(stderr: str) -> str:
    """Pick the most informative line for the report."""
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    for needle in ("panicked at", "Error:", "not yet implemented", "Unknown"):
        for ln in lines:
            if needle.lower() in ln.lower():
                return ln[:300]
    return (lines[-1] if lines else "")[:300]


def make_result(
    game: Path, status: str, summary: str, stderr_tail: str, rc: int, elapsed: float
) -> dict:
    return {
        "id": game_id(game),
        "path": str(game.relative_to(CORPUS)).replace("\\", "/"),
        "name": game.name,
        "size": game.stat().st_size if game.exists() else 0,
        "status": status,
        "summary": summary,
        "returncode": rc,
        "elapsed": round(elapsed, 2),
        "stderr_tail": stderr_tail,
    }


def write_result(game: Path, res: dict) -> None:
    p = result_path(game)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# ---------- report ----------

STATUS_ORDER = [
    "ok_alive",
    "ok_exit",
    "unimplemented",
    "panic",
    "load_error",
    "unsupported_format",
    "runner_error",
]

STATUS_EMOJI = {
    "ok_alive": "🟢",
    "ok_exit": "🟢",
    "unimplemented": "🟡",
    "panic": "🔴",
    "load_error": "🔴",
    "unsupported_format": "⚪",
    "runner_error": "❓",
}


def cmd_report(args: argparse.Namespace) -> int:
    items = []
    for p in sorted(RESULTS.glob("*.json")):
        try:
            items.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"  ! bad result {p.name}: {e}", file=sys.stderr)

    if not items:
        print("no results yet — run `test` first", file=sys.stderr)
        return 1

    counts = Counter(r["status"] for r in items)
    by_status: dict[str, list[dict]] = defaultdict(list)
    for r in items:
        by_status[r["status"]].append(r)

    (ROOT / "report.json").write_text(
        json.dumps(
            {"counts": dict(counts), "results": items}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )

    md = ["# wie compatibility report\n"]
    md.append(f"Total: **{len(items)}** games tested\n")
    md.append("| Status | Count |")
    md.append("|---|---:|")
    for s in STATUS_ORDER:
        if s in counts:
            md.append(f"| {STATUS_EMOJI.get(s,'')} {s} | {counts[s]} |")
    md.append("")

    for s in STATUS_ORDER:
        rows = by_status.get(s, [])
        if not rows:
            continue
        md.append(f"\n## {STATUS_EMOJI.get(s,'')} {s} ({len(rows)})\n")
        md.append("| Game | Size | Time | Detail |")
        md.append("|---|---:|---:|---|")
        for r in sorted(rows, key=lambda x: x["name"].lower()):
            size_kb = r["size"] // 1024
            detail = r["summary"].replace("|", "\\|")
            md.append(f"| `{r['path']}` | {size_kb} KB | {r['elapsed']}s | {detail} |")

    (ROOT / "report.md").write_text("\n".join(md), encoding="utf-8")
    print(f"[report] wrote report.md and report.json ({len(items)} entries)")
    return 0


# ---------- main ----------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download + extract corpus")
    f.add_argument("--redownload", action="store_true")
    f.set_defaults(func=cmd_fetch)

    t = sub.add_parser("test", help="run wie_cli on every game")
    t.add_argument("--wie", required=True, help="path to wie_cli executable")
    t.add_argument("--timeout", type=float, default=25.0)
    t.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="parallel jobs (winit windows pop up - keep 1 unless you don't mind)",
    )
    t.add_argument("--limit", type=int, default=0)
    t.add_argument("--force", action="store_true", help="re-run cached results")
    t.set_defaults(func=cmd_test)

    r = sub.add_parser("report", help="aggregate results into report.md")
    r.set_defaults(func=cmd_report)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
