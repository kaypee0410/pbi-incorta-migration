"""Check that no name from a client report (table, column or measure of a file in samples/ or uploads/) appears in
the files git tracks or is about to commit.

    python tools/check_client_names.py [--min-length 5] [--staged]

Prints file:line and the name for each hit and exits 1 when there is any. Generic words (Date, Amount, Region ...)
are skipped: they are in every report and identify no client. Reads only the model's metadata, never data rows.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GENERIC = {
    "date", "dates", "year", "month", "day", "week", "quarter", "amount", "total", "value", "values", "region",
    "country", "city", "state", "status", "type", "name", "code", "category", "product", "products", "customer",
    "customers", "sales", "store", "stores", "price", "quantity", "qty", "cost", "target", "currency", "key",
    "description", "number", "count", "brand", "entity", "group", "level", "measure", "measures", "table",
    "column", "calendar", "datetable", "month number", "monthname", "month name", "year month", "weekday",
    "average", "comment", "comments", "notes", "remarks", "unit", "units", "rate", "rates", "index", "sort",
    "order", "orders", "item", "items", "supplier", "vendor", "location", "warehouse", "inventory", "stock",
    "revenue", "profit", "margin", "discount", "channel", "segment", "source", "period", "flag", "label",
    "active", "inactive", "reference", "today", "timestamp", "placeholder", "printed", "created by", "display name",
    "fiscal year", "root cause", "conversion", "column1", "column2", "column3", "product code",
}


def client_names(min_length: int) -> set[str]:
    from pbi_insights.extract.model import extract_model
    from pbi_insights.extract.package import open_package
    names = set()
    files = [p for d in ("samples", "uploads") for p in (ROOT / d).glob("**/*")
             if p.suffix.lower() in (".pbix", ".pbit")]
    for f in files:
        try:
            with open_package(f) as pkg:
                model = extract_model(pkg)
        except Exception as ex:  # noqa: BLE001 - one unreadable file must not stop the check
            print(f"(skipped {f.name}: {type(ex).__name__})", file=sys.stderr)
            continue
        for t in model.get("tables", []):
            names.add(t["name"])
        for c in model.get("columns", []):
            names.add(c["column"])
        for m in model.get("measures", []):
            names.add(m["name"])
    out = set()
    for n in names:
        s = str(n).strip()
        if len(s) >= min_length and s.lower() not in GENERIC and not s.lower().startswith(("localdatetable", "datetabletemplate")):
            out.add(s)
    return out


def tracked_files(staged: bool) -> list[Path]:
    cmd = ["git", "diff", "--cached", "--name-only"] if staged else ["git", "ls-files"]
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True)
    return [ROOT / x for x in res.stdout.splitlines() if x and (ROOT / x).is_file()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--min-length", type=int, default=5)
    ap.add_argument("--staged", action="store_true", help="check only the staged files")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    names = client_names(args.min_length)
    if not names:
        print("No client files found in samples/ or uploads/: nothing to check against.")
        return 0
    pats = [(n, re.compile(r"(?<![A-Za-z0-9_])" + re.escape(n) + r"(?![A-Za-z0-9_])", re.I)) for n in names]
    hits = 0
    for f in tracked_files(args.staged):
        if f.suffix.lower() in (".woff2", ".png", ".jpg", ".xlsx", ".pdf", ".zip"):
            continue
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(lines, start=1):
            for n, p in pats:
                if p.search(line):
                    print(f"{f.relative_to(ROOT)}:{i}: {n}")
                    hits += 1
    print(f"{len(names)} client names checked, {hits} hit(s).")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
