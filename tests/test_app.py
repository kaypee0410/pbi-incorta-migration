import io
import time

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app as webapp

    monkeypatch.setattr(webapp, "UPLOADS", tmp_path / "uploads")
    monkeypatch.setattr(webapp, "OUTPUTS", tmp_path / "outputs")
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()


def _run(client, pbit, **form):
    with open(pbit, "rb") as fh:
        r = client.post("/api/jobs", data={"file": (fh, "synthetic.pbit"), **form}, content_type="multipart/form-data")
    assert r.status_code == 202
    job_id = r.json["id"]
    for _ in range(400):
        job = client.get(f"/api/jobs/{job_id}").json
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_page_and_health(client):
    r = client.get("/")
    assert r.status_code == 200 and b"Power BI Extraction Insights" in r.data
    page = r.data.decode().lower()
    for word in ("quicksight", "tableau", "dialect", "claude"):
        assert word not in page
    assert "fonts.googleapis" not in page                 # fonts are bundled: the page works offline
    assert client.get("/healthz").json["ok"] is True


def test_rejects_missing_and_wrong_files(client):
    assert client.post("/api/jobs", data={}).status_code == 400
    r = client.post("/api/jobs", data={"file": (io.BytesIO(b"x"), "notes.txt")})
    assert r.status_code == 400 and "not a Power BI file" in r.json["error"]


def test_job_runs_and_downloads(client, pbit):
    job = _run(client, pbit)
    assert job["status"] == "done", job.get("error")
    v = job["view"]
    assert len(v["insights"]) == 6
    c = v["counts"]
    listed = sum(len(g["measures"]) for g in v["groups"])
    assert listed + v["otherReviewCount"] == c["measures"] - c["translated"]
    assert not v["package"] and "package" not in job["downloads"]
    x = client.get(job["downloads"]["workbook"])
    assert x.status_code == 200 and x.data[:2] == b"PK"
    assert client.get(f"/api/jobs/{job['id']}/download/exe").status_code == 404
    assert client.get("/api/jobs/not-a-job/download/xlsx").status_code == 404


def test_package_history_and_done_state(client, pbit):
    first = _run(client, pbit, incorta="on")
    second = _run(client, pbit, incorta="on")
    assert second["view"]["package"] and "package" in second["downloads"]
    assert len(second["view"]["mvs"]) == second["view"]["counts"]["mvs"]

    hist = client.get("/api/history").json
    assert len(hist) == 1 and [r["id"] for r in hist[0]["runs"]] == [second["id"], first["id"]]
    assert hist[0]["runs"][0]["diff"] == "No changes from the earlier run" and hist[0]["runs"][1]["diff"] == ""

    groups = second["view"]["groups"]
    if groups:
        name = groups[0]["measures"][0]["name"]
        r = client.post(f"/api/jobs/{second['id']}/done", json={"measure": name, "done": True})
        assert r.status_code == 200 and r.json["done"] is True
        again = client.get(f"/api/jobs/{second['id']}").json
        assert again["view"]["groups"][0]["measures"][0]["done"] is True
    assert client.post(f"/api/jobs/{second['id']}/done", json={}).status_code == 400
    assert client.post("/api/jobs/000000000000/done", json={"measure": "x", "done": True}).status_code == 404
