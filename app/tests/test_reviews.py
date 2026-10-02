import json

import pytest
import yaml

from sagent import db
from sagent.core import harness, loops, reviews, runs, scanner
from sagent.core.errors import ValidationError


def _boot(t, **review_overrides):
    harness.bootstrap(t["owner"], t["project"].slug, scanner.scan(t["project"].path),
                      {"coding_agent": "claude", "review_agent": "claude"}, overwrite=True)
    if review_overrides:
        p = t["project"].path + "/.sagent/review.yaml"
        data = yaml.safe_load(open(p))
        data.update(review_overrides)
        open(p, "w").write(yaml.safe_dump(data))


def _review(run):
    runs.wait(run.id, timeout=30, poll=0.3)
    return reviews.ReviewRun.from_row(db.query_one("SELECT * FROM review_runs WHERE run_id = ?", (run.id,)))


def test_review_rejects_bug(team_project, git_project):
    t = team_project
    _boot(t)
    (git_project / "app.py").write_text("print('XXBUGXX')\n")
    run = reviews.start(t["dev"], t["project"].slug)
    spec = json.loads((runs.run_dir(run.id) / "spec.json").read_text())
    assert spec["cmd"][spec["cmd"].index("--permission-mode") + 1] == "plan"  # read-only reviewer
    rr = _review(run)
    assert rr.status == "SUCCESS" and rr.verdict == "reject" and rr.high == 1
    issue = reviews.issues(rr.id)[0]
    assert issue["file"] == "bug.txt" and issue["line"] == 1 and issue["confidence"] == pytest.approx(0.9)
    prompt = (runs.run_dir(run.id) / "prompt.md").read_text()
    assert "## Diff" in prompt and "-print('v1')" in prompt and "Project rules" in prompt


def test_review_approves_clean_change_and_sees_untracked(team_project, git_project):
    t = team_project
    _boot(t)
    (git_project / "new_module.py").write_text("def ok():\n    return 1\n")
    rr = _review(reviews.start(t["dev"], t["project"].slug))
    assert rr.verdict == "approve" and rr.files_changed >= 1
    assert "New file: new_module.py" in (runs.run_dir(rr.run_id) / "prompt.md").read_text()


def test_block_on_overrides_approve(team_project, git_project):
    t = team_project
    _boot(t, block_on=["critical", "high", "medium"])
    (git_project / "app.py").write_text("print('MEDIUMISSUE')\n")
    rr = _review(reviews.start(t["dev"], t["project"].slug))
    assert rr.verdict == "reject" and rr.medium == 1
    i = reviews.issues(rr.id)[0]
    assert i["line"] == 7 and i["confidence"] == 1.0  # clamped


def test_unparseable_review_fails(team_project, git_project):
    t = team_project
    _boot(t)
    (git_project / "app.py").write_text("print('GARBAGE')\n")
    rr = _review(reviews.start(t["dev"], t["project"].slug))
    assert rr.status == "FAILED" and rr.verdict == "reject"


def test_review_input_validation(team_project, git_project):
    import subprocess

    t = team_project
    _boot(t)
    subprocess.run(["git", "-C", str(git_project), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git_project), "-c", "user.email=t@example.com", "-c", "user.name=t",
                    "commit", "-qm", "harness"], check=True)
    with pytest.raises(ValidationError):
        reviews.start(t["dev"], t["project"].slug)  # no changes
    for bad in ("--upload-pack=evil", "HEAD;rm", "", "nonexistent-branch"):
        with pytest.raises(ValidationError):
            reviews.start(t["dev"], t["project"].slug, base_ref=bad)


def test_codex_reviewer_is_read_only(team_project, git_project):
    t = team_project
    _boot(t, provider="codex")
    (git_project / "app.py").write_text("print('x')\n")
    run = reviews.start(t["dev"], t["project"].slug)
    cmd = json.loads((runs.run_dir(run.id) / "spec.json").read_text())["cmd"]
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    runs.wait(run.id, timeout=30, poll=0.3)


def test_extract_json_variants():
    assert reviews.extract_json('noise {"verdict": "approve", "issues": []} trailing')["verdict"] == "approve"
    assert reviews.extract_json('```json\n{"verdict":"reject"}\n```')["verdict"] == "reject"
    assert reviews.extract_json("no json here") is None


def test_standard_loop_review_reject_then_fix(team_project, git_project):
    t = team_project
    _boot(t)
    tests_yaml = t["project"].path + "/.sagent/tests.yaml"
    data = yaml.safe_load(open(tests_yaml))
    data["unit"]["command"] = "true"
    open(tests_yaml, "w").write(yaml.safe_dump(data))
    lr = loops.start(t["dev"], t["project"].slug, "standard", "BUGFILE=bug.txt RMFILE=bug.txt")
    lr = loops.wait(lr.id, timeout=120, poll=0.3)
    assert lr.status == "SUCCESS", lr.error
    seq = [(r["node_key"], r["outcome"]) for r in loops.node_rows(lr.id)]
    assert seq == [("plan", "success"), ("implement", "success"), ("unit", "success"), ("review", "reject"),
                   ("implement", "success"), ("unit", "success"), ("review", "approve"), ("done", "success")]
    second_impl = [r for r in loops.node_rows(lr.id) if r["node_key"] == "implement"][1]["run_id"]
    prompt = (runs.run_dir(second_impl) / "prompt.md").read_text()
    assert "Code review requested changes" in prompt and "contains XXBUGXX" in prompt
    assert "Plan from the previous step" in prompt


def test_reviews_pages(team_project, git_project, new_browser):
    t = team_project
    _boot(t)
    (git_project / "app.py").write_text("print('XXBUGXX')\n")
    slug = t["project"].slug
    d = new_browser()
    d.login("dev1")
    resp = d.post(f"/p/{slug}/reviews", {"base_ref": "HEAD"})
    assert resp.status_code == 302
    rid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    rr = reviews.ReviewRun.from_row(db.query_one("SELECT * FROM review_runs WHERE id = ?", (rid,)))
    runs.wait(rr.run_id, timeout=30, poll=0.3)
    page = d.get(f"/p/{slug}/reviews/{rid}").get_data(as_text=True)
    assert "contains XXBUGXX" in page and "reject" in page
    assert "reject" in d.get(f"/p/{slug}/reviews").get_data(as_text=True)
    v = new_browser()
    v.login("vic")
    assert v.get(f"/p/{slug}/reviews/{rid}").status_code == 200
    assert v.post(f"/p/{slug}/reviews", {"base_ref": "HEAD"}).status_code == 403
