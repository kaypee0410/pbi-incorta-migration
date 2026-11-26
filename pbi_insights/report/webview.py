"""The web page's view of one run: counts, review measures grouped by blocking pattern, the Incorta build plan,
findings, insights and sources.

Built from the run's JSON export (``*_inventory.json``), so new runs and runs made before this page existed
render the same way. In DAX and SQL strings, «...» marks the part the translator could not handle; the page
escapes the text before it highlights the marks.
"""
from __future__ import annotations

import re

MARK_L, MARK_R = "«", "»"
MANUAL_FINDING = "Measures that need manual translation"   # shown as pattern groups instead
OTHER = "Other"

TIME_FUNCS = ("DATEADD", "SAMEPERIODLASTYEAR", "PARALLELPERIOD", "PREVIOUSDAY", "PREVIOUSMONTH", "PREVIOUSQUARTER",
              "PREVIOUSYEAR", "NEXTDAY", "NEXTMONTH", "NEXTQUARTER", "NEXTYEAR", "TOTALYTD", "TOTALQTD", "TOTALMTD",
              "DATESYTD", "DATESQTD", "DATESMTD", "DATESBETWEEN", "DATESINPERIOD", "OPENINGBALANCEMONTH",
              "CLOSINGBALANCEMONTH", "STARTOFMONTH", "ENDOFMONTH", "STARTOFYEAR", "ENDOFYEAR")
ALL_FUNCS = ("REMOVEFILTERS", "ALL", "ALLEXCEPT", "ALLSELECTED", "ALLNOBLANKROW")

# pattern -> (why it blocks the SQL rules, what to do in Incorta)
PATTERNS = {
    "VALUES": ("Used as a filter or iterator, which has no direct SQL equivalent.",
               "Usually becomes a join to a distinct list or an IN subquery."),
    "CALCULATETABLE": ("Builds a filtered table inside the measure.",
                       "Build the table as its own MV, then count or join to it."),
    "SUMMARIZE": ("Groups a table inside the measure.",
                  "Group in a subquery or MV, then aggregate the result."),
    "ADDCOLUMNS": ("Adds computed columns to a table that only exists inside the measure.",
                   "Build the table and its columns as an MV first."),
    "FILTER": ("Filters a table expression row by row.",
               "Write the condition as a WHERE clause on the MV, or as an insight filter."),
    "TOPN": ("Keeps the top rows of a table inside the measure.",
             "No automatic Incorta home: design it by hand (rank in an MV, or an insight filter)."),
    "TREATAS": ("Changes how two tables relate for this one calculation.", "Write the join explicitly in the MV."),
    "USERELATIONSHIP": ("Switches to an inactive relationship for this calculation.",
                        "Write the join on the other key explicitly in the MV."),
    "REMOVEFILTERS / ALL": ("Needs a total at a coarser grain than the row.",
                            "In Incorta use a groupBy() level-based measure; in SQL a window or a separate MV."),
    "ALLSELECTED": ("Removes filters inside the visual but keeps the outer ones.",
                    "Use a window function over the insight's rows, or a level-based measure."),
    "Time intelligence": ("Shifts or accumulates dates (prior period, year to date).",
                          "In Incorta use ago() / toDate() on the date column; in SQL a date-table join."),
    "RANKX": ("Ranks rows of the visual.",
              "In Incorta use rank() / denseRank(); in SQL a window function."),
    "COUNTROWS over a computed table": ("Counts rows of a table that only exists inside the measure.",
                                        "Materialise the computed table as an MV first."),
    "Context transition": ("Evaluates a measure once per row (context transition).",
                           "Pre-aggregate at the row's grain in a subquery or MV, then join it back."),
    "Correlated subquery": ("Depends on the outer row, like a correlated subquery.",
                            "Pre-aggregate per key in an MV and join it back to the rows."),
    "Nested aggregation": ("Aggregates an aggregate.", "Group in a subquery or MV, then aggregate the result."),
    "FILTER(ALL(...))": ("Combines a filter-free total with other filters.",
                         "Compute it in its own MV or a level-based measure."),
    "LOOKUPVALUE": ("Looks a value up in another table.", "Join the lookup table in the MV."),
    "IN over a table": ("Compares with the rows of a table expression.", "Rewrite as a join or an IN subquery."),
    "Unknown reference": ("Refers to a column, table or measure the model doesn't have.",
                          "Check the name in Power BI; the reference may be broken there too."),
    "DAX could not be parsed": ("The DAX text could not be read.", "Rewrite it by hand from the DAX."),
}
GROUPED_X = ("{f} iterates a table grouped or computed inside the measure.",
             "Group in a subquery or MV, then aggregate the result.")
UNKNOWN_FUNC = ("{f} has no rule in the translator yet.",
                "Write it by hand from the DAX; the Measures sheet has the partial SQL.")


def pattern_of(reason: str) -> str:
    """One stable pattern key for a review reason from the DAX translator."""
    r = (reason or "").strip()
    low = r.lower()
    if low.startswith("time intelligence") or " with a fiscal year end" in low:
        return "Time intelligence"
    if r.startswith("REMOVEFILTERS / ALL"):
        return "REMOVEFILTERS / ALL"
    if r.startswith("RANKX"):
        return "RANKX"
    if r.startswith("LOOKUPVALUE"):
        return "LOOKUPVALUE"
    if r.startswith("COUNTROWS over a computed table"):
        return "COUNTROWS over a computed table"
    m = re.match(r"^(\w+) over a grouped or computed table", r)
    if m:
        return f"{m.group(1).upper()} over a grouped table"
    if "context transition" in low:
        return "Context transition"
    if "depend on an outer row" in low:
        return "Correlated subquery"
    if "nested aggregation" in low:
        return "Nested aggregation"
    if "filter(all(" in low:
        return "FILTER(ALL(...))"
    if low.startswith(("in against a table", "multi-column in")):
        return "IN over a table"
    if low.startswith(("unknown ", "circular ")):
        return "Unknown reference"
    if "could not be parsed" in low:
        return "DAX could not be parsed"
    m = re.match(r"^(\w+) (is not translated automatically|changes how tables relate|removes filters)", r)
    if m:
        return m.group(1).upper()
    return OTHER


def _why_fix(pattern: str) -> tuple[str, str]:
    if pattern in PATTERNS:
        return PATTERNS[pattern]
    m = re.match(r"^(\w+) over a grouped table$", pattern)
    if m:
        return GROUPED_X[0].format(f=m.group(1)), GROUPED_X[1]
    return UNKNOWN_FUNC[0].format(f=pattern), UNKNOWN_FUNC[1]


def _tokens(pattern: str, reason: str) -> list[str]:
    if pattern == "Time intelligence":
        return list(TIME_FUNCS)
    if pattern == "REMOVEFILTERS / ALL":
        return list(ALL_FUNCS)
    if pattern == "FILTER(ALL(...))":
        return ["FILTER"]
    if pattern == "IN over a table":
        return ["IN"]
    head = pattern.split(" ")[0]
    out = [head] if re.fullmatch(r"[A-Z][A-Z0-9.]*", head) else []
    out += [w for w in re.findall(r"\b[A-Z][A-Z0-9]{2,}\b", reason or "") if w not in out]
    return out


def _clean(s) -> str:
    return str(s or "").replace(MARK_L, "‹").replace(MARK_R, "›")


def mark_dax(dax: str, pattern: str, reason: str, blocked: set | frozenset = frozenset()) -> str:
    """Wrap the DAX functions behind the pattern in «»; measure references named in the reason too. When the
    pattern sits in a measure this one uses, wrap the references to those blocked measures instead."""
    s = _clean(dax)
    toks = _tokens(pattern, reason)
    if toks:
        rx = re.compile(r"(?<![\w.])(" + "|".join(re.escape(t) for t in sorted(toks, key=len, reverse=True))
                        + r")(?=\s*\()", re.I)
        s = rx.sub(lambda m: MARK_L + m.group(1) + MARK_R, s)
    for ref in re.findall(r"\[[^\]]+\]", reason or ""):
        if ref in s and MARK_L + ref not in s:
            s = s.replace(ref, MARK_L + ref + MARK_R, 1)
    if MARK_L not in s and blocked:
        low = {n.lower() for n in blocked}
        s = re.sub(r"\[([^\]]+)\]", lambda m: MARK_L + m.group(0) + MARK_R if m.group(1).strip().lower() in low
                   else m.group(0), s)
    return s


def mark_sql(sql: str, reason: str) -> str:
    """Wrap each review placeholder (NULL /* REVIEW: ... */) in «»; no SQL at all becomes one marked comment."""
    s = _clean(sql).strip()
    if not s:
        return f"{MARK_L}-- no draft SQL: {_clean(reason)}{MARK_R}"
    return re.sub(r"(?:NULL\s*)?/\*\s*REVIEW:.*?\*/", lambda m: MARK_L + m.group(0) + MARK_R, s, flags=re.S)


def _status(tr: dict) -> str:
    return str((tr or {}).get("status") or "")


def counts_from_summary(s: dict) -> dict:
    s = s or {}
    inc = s.get("incorta") or {}
    return {"tabs": s.get("tabs") or 0, "insights": s.get("data_insights") or 0,
            "staticItems": s.get("static_items") or 0, "tables": s.get("tables") or 0,
            "relationships": s.get("relationships") or 0, "measures": s.get("measures") or 0,
            "translated": s.get("measures_translated") or 0, "findings": s.get("findings") or 0,
            "high": s.get("findings_high") or 0, "queries": s.get("sql_test_runs"),
            "mvs": inc.get("mvs"), "joins": inc.get("joins")}


def _summary_line(c: dict) -> str:
    return f"{c['tabs']} tabs, {c['insights']} insights, {c['measures']} measures"


DIFF_FIELDS = (("translated", "measure translated", "measures translated"), ("measures", "measure", "measures"),
               ("tabs", "tab", "tabs"), ("insights", "insight", "insights"), ("findings", "finding", "findings"),
               ("mvs", "MV", "MVs"), ("joins", "join", "joins"))


def diff_counts(new: dict, old: dict) -> str:
    """'No changes from the earlier run' or e.g. '2 more measures translated, 1 fewer finding'."""
    parts = []
    for key, one, many in DIFF_FIELDS:
        a, b = new.get(key), old.get(key)
        if a is None or b is None or a == b:
            continue
        n = abs(a - b)
        parts.append(f"{n} {'more' if a > b else 'fewer'} {one if n == 1 else many}")
    return ", ".join(parts) if parts else "No changes from the earlier run"


def build_view(data: dict) -> dict:
    """The page's RESULT object (without history and downloads, which the app adds)."""
    s = data.get("summary") or {}
    counts = counts_from_summary(s)
    plans = data.get("plans") or []
    page_of = {p.get("id"): p.get("page") or "" for p in plans}
    runs = ((data.get("checks") or {}).get("test_run")) or {}
    inc = data.get("incorta") or None
    if inc:
        counts["tables"] = (inc.get("summary") or {}).get("tables_loaded", counts["tables"])

    # ---- measures, grouped by the first pattern that blocked them
    blocked = {m.get("name") or "" for m in data.get("measures") or []
               if ((m.get("translation") or {}).get("reviews"))}
    groups: dict[str, dict] = {}
    other = 0
    measure_rows = []
    for m in data.get("measures") or []:
        tr = m.get("translation") or {}
        reviews = [str(r) for r in tr.get("reviews") or []]
        pattern = pattern_of(reviews[0]) if reviews else ""
        measure_rows.append({"name": m.get("name") or "", "table": m.get("table") or "", "scope": m.get("scope") or "",
                             "status": "Needs review" if reviews else "Translated", "pattern": pattern})
        if not reviews:
            continue
        if pattern == OTHER:
            other += 1
            continue
        g = groups.get(pattern)
        if g is None:
            why, fix = _why_fix(pattern)
            g = groups[pattern] = {"pattern": pattern, "why": why, "fix": fix, "measures": []}
        g["measures"].append({"name": m.get("name") or "", "reason": reviews[0],
                              "dax": mark_dax(m.get("dax") or "", pattern, reviews[0], blocked - {m.get("name")}),
                              "sql": mark_sql(m.get("sql") or "", reviews[0])})
    ordered = sorted(groups.values(), key=lambda g: (-len(g["measures"]), g["pattern"]))

    # ---- Incorta build plan (dependency order: Power Query MVs by depth first, then the rest as planned)
    mvs = []
    schema = str(((inc or {}).get("summary") or {}).get("schema") or "")
    strip = (lambda x: x[len(schema) + 1:] if schema and str(x).startswith(schema + ".") else str(x))
    for i, mv in sorted(enumerate((inc or {}).get("mvs") or []),
                        key=lambda t: (t[1].get("kind") != "Power Query MV", t[1].get("depth") or 0, t[0])):
        tabs = []
        for pid in mv.get("serves") or []:
            pg = page_of.get(pid)
            if pg and pg not in tabs:
                tabs.append(pg)
        feeds = ", ".join(tabs) or (("tables: " + ", ".join(mv["feeds"])) if mv.get("feeds") else "-")
        ready = mv.get("status") != "Needs design"
        note = ""
        if not ready:
            rv = [str(r) for r in mv.get("reviews") or []]
            p = pattern_of(rv[0]) if rv else OTHER
            note = p if p != OTHER else (rv[0] if rv else (mv.get("reason") or "needs a design decision"))
            note = note if len(note) <= 90 else note[:87] + "..."
        mvs.append({"name": mv.get("name") or "", "kind": mv.get("kind") or "", "from": ", ".join(strip(r) for r in mv.get("reads") or []) or "-",
                    "feeds": feeds, "ready": ready, "note": note})

    sev_order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    findings = [{"severity": str(f.get("severity") or "Info").lower(), "area": f.get("area") or "",
                 "title": f.get("finding") or "", "detail": f.get("detail") or "", "rec": f.get("recommendation") or ""}
                for f in data.get("findings") or [] if f.get("finding") != MANUAL_FINDING]
    findings.sort(key=lambda f: sev_order.get(f["severity"], 4))

    insights = []
    for p in plans:
        if p.get("approach") == "Static":
            continue
        r = runs.get(p.get("id")) or {}
        insights.append({"id": p.get("id") or "", "tab": p.get("page") or "", "name": p.get("title") or p.get("id") or "",
                         "type": p.get("generic_type") or "", "rows": r.get("rows"), "run": r.get("status"),
                         "ok": p.get("approach") != "Needs review"})

    tables = {t.get("name"): t for t in (data.get("model") or {}).get("tables") or []}
    sources = []
    for name, src in (data.get("sources") or {}).items():
        t = tables.get(name) or {}
        if t.get("auto_date"):
            continue
        mq = (src or {}).get("m") or {}
        objs = mq.get("source_objects") or ((src or {}).get("sql") or {}).get("objects") or []
        detail = ", ".join(map(str, objs)) or ".".join(x for x in (mq.get("server"), mq.get("database")) if x)
        sources.append({"name": name, "kind": mq.get("connector") or ("DAX" if t.get("calculated") else "-"),
                        "detail": detail or "-", "steps": len(mq.get("steps") or [])})

    return {"file": s.get("file") or "", "sizeMB": s.get("file_size_mb"), "seconds": s.get("seconds"),
            "counts": counts, "groups": ordered, "otherReviewCount": other, "measures": measure_rows,
            "mvs": mvs, "findings": findings, "insights": insights, "sources": sources,
            "package": bool(inc), "schema": schema, "warnings": [str(w) for w in s.get("warnings") or []],
            "summaryLine": _summary_line(counts)}
