"""Regression of the measure translation on every Power BI file in samples/: old code (a git commit) vs the current
working tree, compared by measure name.

    python tools/regress_measures.py [--before 0d8b5d6] [--data] [--files samples/*.pbix] [--out outputs/regress]

For each file: translated measures before / after, measures whose status went down (Translated -> Needs review),
measures whose generic SQL changed, and (with --data) how many insight queries run on the saved rows. The old code
runs from a temporary git worktree, each run in its own process. Only metadata (measure names, statuses, SQL) and
run counts are read and written; no data row leaves the process.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OK = ("Translated", "Translated with notes")

RUNNER = r'''
import json, sys, logging
logging.disable(logging.CRITICAL)
sys.path.insert(0, sys.argv[1])
from pbi_insights.pipeline import Options, analyze
d = analyze(sys.argv[2], Options(read_data=sys.argv[3] == "1"))
out = {"measures": {}, "runs": None}
for m in d["measures"]:
    tr = m["translation"]
    out["measures"][m["name"]] = {"status": tr.status, "pattern": m.get("pattern"), "sql": m.get("sql") or "",
                                  "scope": m.get("scope")}
tr = (d.get("checks") or {}).get("test_run")
if tr:
    counts = {}
    for v in tr.values():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    out["runs"] = counts
json.dump(out, open(sys.argv[4], "w", encoding="utf-8"), ensure_ascii=False)
'''


def run(code_root: Path, pbix: Path, data: bool, out: Path) -> dict | None:
    script = out.with_suffix(".runner.py")
    script.write_text(RUNNER, encoding="utf-8")
    res = subprocess.run([sys.executable, str(script), str(code_root), str(pbix), "1" if data else "0", str(out)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
    if res.returncode != 0 or not out.exists():
        tail = (res.stderr or "").strip().splitlines()[-1:] or ["no output"]
        print(f"   run failed ({code_root.name}): {tail[0][:200]}")
        return None
    return json.loads(out.read_text(encoding="utf-8"))


def compare(before: dict, after: dict) -> dict:
    b, a = before["measures"], after["measures"]
    drops = sorted(n for n in a if n in b and b[n]["status"] in OK and a[n]["status"] not in OK)
    gains = sorted(n for n in a if n in b and b[n]["status"] not in OK and a[n]["status"] in OK)
    sql = sorted(n for n in a if n in b and b[n]["sql"] != a[n]["sql"])
    return {"before": sum(1 for x in b.values() if x["status"] in OK), "after": sum(1 for x in a.values() if x["status"] in OK),
            "total": len(a), "drops": drops, "gains": gains, "sql_changed": sql,
            "runs_before": before.get("runs"), "runs_after": after.get("runs")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--before", default="0d8b5d6", help="git revision of the old code (default: 0d8b5d6)")
    ap.add_argument("--data", action="store_true", help="also test-run every insight query on the saved rows")
    ap.add_argument("--files", nargs="*", help="files to run (default: every .pbix / .pbit in samples/)")
    ap.add_argument("--out", default="outputs/regress", help="folder for the JSON report (git-ignored)")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    files = [Path(f) for f in args.files] if args.files else sorted(
        p for p in (ROOT / "samples").glob("*") if p.suffix.lower() in (".pbix", ".pbit"))
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="regress_"))
    wt = tmp / "before"
    subprocess.run(["git", "worktree", "add", "--detach", str(wt), args.before], cwd=ROOT, check=True,
                   capture_output=True)
    report = {}
    try:
        for f in files:
            print(f"{f.name}")
            b = run(wt, f, args.data, tmp / f"{f.stem}.before.json")
            a = run(ROOT, f, args.data, tmp / f"{f.stem}.after.json")
            if b is None or a is None:
                report[f.name] = {"error": "run failed"}
                continue
            report[f.name] = c = compare(b, a)
            print(f"   translated {c['before']} -> {c['after']} of {c['total']}; status drops {len(c['drops'])}; "
                  f"SQL changed {len(c['sql_changed'])}; insights run {c['runs_before']} -> {c['runs_after']}")
            for n in c["drops"]:
                print(f"   DROP: {n}: {b['measures'][n]['pattern']} -> {a['measures'][n]['pattern']}")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=ROOT, capture_output=True)
    path = out_dir / f"regress_{args.before}.json"
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport: {path.relative_to(ROOT)}")
    return 1 if any(r.get("drops") or r.get("error") for r in report.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
