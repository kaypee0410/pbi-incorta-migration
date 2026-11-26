"""The web page's view: pattern keys, «» marks, history diff, and robustness on thin or odd exports."""
from pbi_insights.report.webview import build_view, diff_counts, mark_dax, mark_sql, pattern_of


def test_pattern_keys():
    assert pattern_of("VALUES is not translated automatically") == "VALUES"
    assert pattern_of("time intelligence (1 month back): needs a date-offset join to the date table") == "Time intelligence"
    assert pattern_of("REMOVEFILTERS / ALL: needs a total at a coarser grain (x)") == "REMOVEFILTERS / ALL"
    assert pattern_of("SUMX over a grouped or computed table: needs a GROUP BY subquery") == "SUMX over a grouped table"
    assert pattern_of("COUNTROWS over a computed table") == "COUNTROWS over a computed table"
    assert pattern_of("TREATAS changes how tables relate: build the join explicitly") == "TREATAS"
    assert pattern_of("measure [X] evaluated per row (context transition): needs a grouped subquery") == "Context transition"
    assert pattern_of("something new") == "Other"


def test_marks_dax_and_sql():
    dax = "SUMX(values(Item[SKU]), [Qty]) + 1"
    assert mark_dax(dax, "VALUES", "VALUES is not translated automatically") == "SUMX(«values»(Item[SKU]), [Qty]) + 1"
    # the blocking function lives in a measure this one uses: mark that reference
    assert mark_dax("[Sales LY] * 2", "Time intelligence", "time intelligence (1 year back)", {"Sales LY"}) == "«[Sales LY]» * 2"
    # existing guillemets in the source never leak through as marks
    assert "«" not in mark_dax("\"«x»\"", "Other", "")
    sql = "SUM(a) - COALESCE(NULL /* REVIEW: time intelligence */, 0)"
    assert mark_sql(sql, "r") == "SUM(a) - COALESCE(«NULL /* REVIEW: time intelligence */», 0)"
    assert mark_sql("", "why").startswith("«-- no draft SQL")


def test_diff_counts():
    a = {"translated": 8, "measures": 43, "tabs": 14, "insights": 67, "findings": 19, "mvs": 26, "joins": 7}
    assert diff_counts(a, dict(a)) == "No changes from the earlier run"
    assert diff_counts({**a, "translated": 10, "findings": 18}, a) == "2 more measures translated, 1 fewer finding"


def test_build_view_on_empty_and_odd_exports():
    v = build_view({})
    assert v["groups"] == [] and v["counts"]["measures"] == 0 and not v["package"]
    odd = {"summary": {"measures": 2, "measures_translated": 1}, "measures": [
        {"name": "A", "dax": "VALUES(T[c])", "translation": {"reviews": ["VALUES is not translated automatically"]}},
        {"name": None, "translation": None}], "findings": [{"severity": None}], "sources": {"T": None},
        "incorta": {"mvs": [{"status": "Needs design", "reviews": ["x" * 200]}]}}
    v = build_view(odd)
    assert v["groups"][0]["measures"][0]["dax"] == "«VALUES»(T[c])"
    assert v["mvs"][0]["note"].endswith("...") and v["findings"][0]["severity"] == "info"
