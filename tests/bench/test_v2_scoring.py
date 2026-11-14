"""bench/v2 scoring and results.json on synthetic data (no stack, no network)."""

import json

import pytest

from bench.v2 import scoring
from bench.v2.client import load_key, presenter_answer
from bench.v2.run import BROWSER_AGENT_CALLS, build_results, order

LEVER = {"file": "lever/1.html.gz", "platform": "lever", "site": "jobs.lever.co",
         "url": "https://jobs.lever.co/acme/1", "title": "Software Engineer", "company": "Acme"}
ASHBY = {"file": "ashby/1.html.gz", "platform": "ashby", "site": "jobs.ashbyhq.com",
         "url": "https://jobs.ashbyhq.com/acme/2", "title": "Designer", "company": "Acme"}
SR = {"file": "smartrecruiters/1.html.gz", "platform": "smartrecruiters", "site": "jobs.smartrecruiters.com",
      "url": "https://jobs.smartrecruiters.com/Acme/3", "title": "Analyst", "company": "Acme Group"}


def form_row(entry, title=None, company=None, field="Company"):
    return {"Email": "demo@example.com", "Job Title": title or entry["title"], field: company or entry["company"],
            "Link": entry["url"], "Date Applied": "2026-11-14", "_ts": "2026-11-14T14:00:00"}


def tracker_row(label, status="success", learn=None, calls=0):
    return {"label": label, "status": status, "reason": None, "parser": "p", "version": 1, "routed_by": "match",
            "learn_thread_id": learn, "ai_calls": calls}


def raw_run(rows, postings):
    return {"form_rows": rows, "teach": {"seconds": 0.9}, "settings": {"agent": "scripted"},
            "layouts_before": [{"parser_id": "lever", "site": "jobs.lever.co"}],
            "postings": [{"n": i, "entry": e, "canonical_url": e["url"], "row": r, "seconds": s, "asked": a}
                         for i, (e, r, s, a) in enumerate(postings, 1)]}


# -- scoring ------------------------------------------------------------------------------------

def test_right_write():
    s = scoring.score_posting(LEVER, LEVER["url"], [form_row(LEVER, title="  software   engineer ")], held=False)
    assert (s["outcome"], s["correct"], s["bad_write"]) == ("right_write", True, False)


def test_wrong_row_counts_as_bad_write():
    rows = [form_row(LEVER, title="Apply for this job")]
    s = scoring.score_posting(LEVER, LEVER["url"], rows, held=False)
    assert (s["outcome"], s["correct"], s["bad_write"]) == ("wrong_write", False, True)
    # wrong company too
    s = scoring.score_posting(LEVER, LEVER["url"], [form_row(LEVER, company="Lever")], held=False)
    assert s["bad_write"]
    # a second row for the same posting is a bad write even if both are right
    s = scoring.score_posting(LEVER, LEVER["url"], [form_row(LEVER), form_row(LEVER)], held=False)
    assert s["outcome"] == "wrong_write" and s["rows"] == 2
    # ... and it shows up in the summary and the results' numbers
    res = build_results(raw_run(rows, [(LEVER, tracker_row("code"), 1.0, [])]), learn_calls=8,
                        learn_calls_kind="estimate", learn_calls_source="test")
    by = {m["name"]: m for m in res["numbers"]}
    assert by["wrong_rows_written"]["value"] == 1 and by["accuracy"]["value"] == 0
    assert res["postings"][0]["correct"] is False


def test_held_and_dropped():
    s = scoring.score_posting(SR, SR["url"], [], held=True)
    assert (s["outcome"], s["correct"], s["bad_write"]) == ("held", None, False)
    s = scoring.score_posting(SR, SR["url"], [], held=False)
    assert (s["outcome"], s["correct"]) == ("dropped", False)


def test_accept_list_and_renamed_company_column():
    e = dict(LEVER, accept={"company": ["Acme Inc."]})
    s = scoring.score_posting(e, e["url"], [form_row(e, company="Acme Inc.", field="Employer")], held=False)
    assert s["outcome"] == "right_write"


def test_rows_matched_by_link_only():
    s = scoring.score_posting(LEVER, LEVER["url"], [form_row(ASHBY)], held=False)
    assert s["outcome"] == "dropped"


# -- series -------------------------------------------------------------------------------------

def test_cumulative_series():
    assert scoring.cumulative([0, 8, 0, 0, 8, 0]) == [0, 8, 8, 8, 16, 16]
    assert scoring.cumulative([]) == []
    postings = [{"learn_thread_id": None}, {"learn_thread_id": "learn-a", "thread_model_calls": 0},
                {"learn_thread_id": None}, {"learn_thread_id": "learn-b", "thread_model_calls": 5}]
    assert scoring.patchwork_calls(postings, per_learn=8) == [0, 8, 0, 5]  # a thread's own count wins
    assert scoring.cumulative(scoring.browser_agent_calls(4, 6)) == [6, 12, 18, 24]

    raw = raw_run([form_row(LEVER), form_row(ASHBY)],
                  [(LEVER, tracker_row("code"), 1.1, []),
                   (ASHBY, tracker_row("learned_now", learn="learn-1"), 4.0, []),
                   (dict(ASHBY, file="ashby/2.html.gz", url="https://jobs.ashbyhq.com/acme/9"),
                    tracker_row("code"), 1.2, [])])
    res = build_results(raw, learn_calls=6, learn_calls_kind="measured (earlier run)", learn_calls_source="W3-7")
    assert res["series"]["patchwork_cumulative_ai_calls"] == [0, 6, 6]
    assert res["series"]["browser_agent_cumulative_ai_calls"] == [BROWSER_AGENT_CALLS * k for k in (1, 2, 3)]
    assert [p["ai_calls_kind"] for p in res["postings"]] == ["measured", "measured (earlier run)", "measured"]
    assert res["series_meta"]["patchwork_cumulative_ai_calls"]["kind"] == "measured (earlier run)"
    # the third posting has no form row: dropped, not right
    assert [p["outcome"] for p in res["postings"]] == ["right_write", "right_write", "dropped"]


# -- labels -------------------------------------------------------------------------------------

def test_every_number_has_kind_and_source():
    raw = raw_run([form_row(LEVER)], [(LEVER, tracker_row("code"), 1.0, []),
                                      (SR, tracker_row("held", status="held", learn="learn-2"), 3.0, ["ask_user"])])
    res = build_results(raw, learn_calls=8, learn_calls_kind="estimate", learn_calls_source="V10 budget")
    assert scoring.check_results(res) == []
    for m in res["numbers"]:
        assert m["kind"] in scoring.KINDS and m["source"] and m["unit"]
    by = {m["name"]: m for m in res["numbers"]}
    assert by["browser_agent_ai_calls_per_posting"]["kind"] == "estimate"
    assert "assumption" in json.dumps(res["series_meta"]["browser_agent_cumulative_ai_calls"])
    assert by["ai_calls_per_learn"]["kind"] == "estimate"
    assert by["seconds_per_learn_median"]["kind"] == "not measured"  # no learned_now row in this run
    assert by["learns_that_asked_a_person"]["value"] == 1
    json.dumps(res)  # serialisable

    # tampering is caught
    bad = json.loads(json.dumps(res))
    bad["numbers"][0]["kind"] = "guess"
    del bad["numbers"][1]["source"]
    bad["series_meta"]["browser_agent_cumulative_ai_calls"].pop("assumption")
    bad["series"]["patchwork_cumulative_ai_calls"].append(99)
    problems = scoring.check_results(bad)
    assert len(problems) == 4, problems


def test_metric_refuses_unlabelled():
    with pytest.raises(ValueError):
        scoring.metric("x", 1, "s", "guess", "src")
    with pytest.raises(ValueError):
        scoring.metric("x", 1, "s", "measured", "")
    with pytest.raises(ValueError):
        scoring.metric("x", None, "s", "measured", "src")
    with pytest.raises(ValueError):
        scoring.metric("x", 3, "s", "not measured", "src")


# -- order and answers --------------------------------------------------------------------------

def test_order_is_fixed_and_mixes_platforms():
    key = load_key()
    a, b = order(key), order(key)
    assert [e["file"] for e in a] == [e["file"] for e in b]
    assert sorted(e["file"] for e in a) == sorted(e["file"] for e in key)
    assert len({e["platform"] for e in a[:10]}) >= 3


def test_presenter_answer_picks_the_answer_key_value():
    ans, how = presenter_answer("Which is the job title?", ["Sorry, Internet Explorer 11 is no longer supported",
                                                            "Analyst"], SR)
    assert ans == "Analyst" and "title" in how
    assert presenter_answer("Which email?", [], None)[0] == "demo@example.com"
