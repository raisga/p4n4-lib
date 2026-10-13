"""Tests: System 1 question sets, evaluation metrics, fitting and the decision log."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from p4n4_lib import system1 as s1

SPEC = {
    "version": 2,
    "questions": {
        "cause": {
            "type": "choice",
            "instructions": "What is the most likely cause?",
            "criteria": {"thermal": "hot", "electrical": "power", "unknown": "cannot tell"},
        }
    },
    "thresholds": {"cause": 0.5},
    "eval": {
        "min_examples": 4,
        "no_answer_keys": {"cause": ["unknown"]},
        "gates": {"cause": {"min_coverage": 0.5}},
    },
}


def _write_set(tmp_path: Path, spec=SPEC, examples=()) -> s1.QuestionSet:
    (tmp_path / "triage.json").write_text(json.dumps(spec))
    if examples:
        (tmp_path / "triage.eval.jsonl").write_text(
            "\n".join(json.dumps(e) for e in examples) + "\n"
        )
    return s1.load_set(tmp_path / "triage.json")


def _answer(choice, confidence, abstain=None, probabilities=None, answer_confidence=None):
    return {
        "cause": {
            "choice": choice,
            "confidence": confidence,
            "answer_confidence": confidence if answer_confidence is None else answer_confidence,
            "abstain": confidence < 0.5 if abstain is None else abstain,
            "probabilities": probabilities or {choice: 1.0},
        }
    }


def _ex(expect, source="history", line=1):
    return s1.Example({"alert": "x"}, {"cause": expect}, source, line)


# ── Sets ─────────────────────────────────────────────────────────────────────


def test_load_set(tmp_path):
    qset = _write_set(tmp_path)
    assert qset.name == "triage" and qset.version == 2
    assert qset.options("cause") == ["thermal", "electrical", "unknown"]
    assert qset.no_answer_keys("cause") == {"unknown"}
    assert qset.gates("cause") == {**s1.DEFAULT_GATES, "min_coverage": 0.5}
    assert qset.min_examples == 4
    assert qset.eval_path == tmp_path / "triage.eval.jsonl"


@pytest.mark.parametrize(
    "change, message",
    [
        ({"version": 0}, "positive integer"),
        ({"version": True}, "positive integer"),
        ({"questions": {}}, "non-empty"),
        ({"questions": {"q": {**SPEC["questions"]["cause"], "type": "noul"}}}, "type 'choice'"),
        (
            {"questions": {"q": {**SPEC["questions"]["cause"], "criteria": {"a": "x"}}}},
            "2 'criteria'",
        ),
        ({"thresholds": {"cause": 1.5}}, "lie in [0, 1]"),
        ({"thresholds": {"nope": 0.5}}, "must name a question"),
        ({"threshold_on": "top"}, "threshold_on"),
        ({"surprise": 1}, "unknown keys"),
        ({"eval": {"gates": {"cause": {"min_cover": 0.5}}}}, "gates for 'cause'"),
        ({"eval": {"no_answer_keys": {"cause": ["other"]}}}, "no_answer_keys"),
        ({"eval": {"extra": 1}}, "'eval' takes only"),
    ],
)
def test_bad_sets(tmp_path, change, message):
    with pytest.raises(s1.QuestionSetError, match=message.replace("[", r"\[").replace("(", r"\(")):
        _write_set(tmp_path, {**SPEC, **change})


def test_list_sets_skips_eval_files(tmp_path):
    _write_set(tmp_path, examples=[{"state": {}, "expect": {"cause": None}}])
    assert [s.name for s in s1.list_sets(tmp_path)] == ["triage"]
    assert s1.list_sets(tmp_path / "missing") == []


def test_load_examples(tmp_path):
    qset = _write_set(
        tmp_path,
        examples=[
            {"state": {"alert": "a"}, "expect": {"cause": "thermal"}, "source": "history"},
            {"state": {"alert": "b"}, "expect": {"cause": None}},
        ],
    )
    examples = s1.load_examples(qset)
    assert [(e.expect["cause"], e.source, e.line) for e in examples] == [
        ("thermal", "history", 1),
        (None, "", 2),
    ]


@pytest.mark.parametrize(
    "line, message",
    [
        ({"state": {}, "expect": {"nope": "thermal"}}, "not a question"),
        ({"state": {}, "expect": {"cause": "cold"}}, "not an option"),
        ({"state": {}, "expect": {"cause": "unknown"}}, "expect null"),
        ({"expect": {"cause": None}}, "needs 'state'"),
    ],
)
def test_bad_examples(tmp_path, line, message):
    qset = _write_set(tmp_path, examples=[line])
    with pytest.raises(s1.QuestionSetError, match=message):
        s1.load_examples(qset)


def test_missing_eval_file(tmp_path):
    with pytest.raises(s1.QuestionSetError, match="no evaluation file"):
        s1.load_examples(_write_set(tmp_path))


# ── Metrics and gates ────────────────────────────────────────────────────────


def test_metrics_follow_the_doc():
    """§10.2: coverage over keyed examples; answered accuracy over answered ones; confident-wrong
    counts wrong answers and any answer on a null example, over all examples."""
    qset = s1.parse_set("triage", SPEC)
    examples = [_ex("thermal"), _ex("thermal"), _ex("electrical"), _ex("electrical"), _ex(None)]
    answers = [
        _answer("thermal", 0.9),  # right
        _answer("electrical", 0.8),  # wrong
        _answer("electrical", 0.2),  # abstained
        _answer("unknown", 0.9),  # a no-answer key: not answered
        _answer("thermal", 0.7),  # answered a null example
    ]
    m = s1.evaluate(qset, examples, answers, gates=False).questions[0].metrics
    assert (m.n1, m.n0, m.answered, m.right, m.answered_null) == (4, 1, 2, 1, 1)
    assert m.coverage == 0.5
    assert m.answered_accuracy == 0.5
    assert m.confident_wrong == pytest.approx(2 / 5)
    assert m.null_abstention == 0.0


def test_gates_and_sizes():
    qset = s1.parse_set("triage", SPEC)
    examples = [_ex("thermal"), _ex(None)]
    report = s1.evaluate(qset, examples, [_answer("thermal", 0.9), _answer("unknown", 0.9)])
    assert not report.passed
    assert any("2 examples, the set asks for at least 4" in f for f in report.failures)
    assert any("fewer than 5 examples for thermal, electrical" in f for f in report.failures)
    assert any("1 null examples" in f for f in report.failures)
    # The metrics themselves pass
    assert report.questions[0].passed


def test_gates_pass_on_enough_good_examples():
    qset = s1.parse_set("triage", SPEC)
    examples = [_ex("thermal")] * 5 + [_ex("electrical")] * 5 + [_ex(None)] * 10
    answers = [_answer("thermal", 0.9)] * 5 + [_answer("electrical", 0.9)] * 5
    answers += [_answer("unknown", 0.95)] * 10
    # Nearest rank: p95 of 20 latencies is the 19th
    report = s1.evaluate(qset, examples, answers, latencies_ms=[10.0] * 18 + [100.0] * 2)
    assert report.passed, report.failures
    assert report.latency_ms == {"p50": 10.0, "p95": 100.0}
    q = report.questions[0]
    assert q.confusion["null"] == {"abstain": 10}
    assert q.confusion["thermal"] == {"thermal": 5}


def test_gate_failures_are_named():
    qset = s1.parse_set("triage", SPEC)
    examples = [_ex("thermal")] * 5 + [_ex("electrical")] * 5 + [_ex(None)] * 10
    answers = [_answer("electrical", 0.9)] * 10 + [_answer("thermal", 0.9)] * 10
    failures = s1.evaluate(qset, examples, answers).failures
    assert any(f.startswith("cause: answered accuracy 0.50") for f in failures)
    assert any(f.startswith("cause: confident-wrong rate 0.750") for f in failures)


def test_by_source():
    qset = s1.parse_set("triage", SPEC)
    examples = [_ex("thermal", "history"), _ex("thermal", "feedback")]
    answers = [_answer("thermal", 0.9), _answer("thermal", 0.1)]
    q = s1.evaluate(qset, examples, answers, gates=False).questions[0]
    assert q.by_source["history"].coverage == 1.0
    assert q.by_source["feedback"].coverage == 0.0


def _lang_ex(expect, lang):
    return s1.Example({"alert": "x"}, {"cause": expect}, "history", 1, lang)


def test_gates_hold_in_every_language():
    qset = s1.parse_set("triage", SPEC)
    en = [_lang_ex("thermal", "en")] * 10 + [_lang_ex(None, "en")] * 5
    es = [_lang_ex("thermal", "es")] * 10 + [_lang_ex(None, "es")] * 5
    right, wrong = _answer("thermal", 0.9), _answer("electrical", 0.9)
    abstain = _answer("thermal", 0.1)
    # Fine in English; Spanish answers a third of its examples wrong
    answers = [right] * 10 + [abstain] * 5 + [right] * 6 + [wrong] * 4 + [abstain] * 5
    report = s1.evaluate(qset, en + es, answers, gates=True)
    (q,) = report.questions
    assert set(q.by_lang) == {"en", "es"}
    assert q.by_lang["en"].confident_wrong == 0
    assert any(f.startswith("cause [es]: confident-wrong") for f in q.failures)
    assert not any("[en]" in f for f in q.failures)
    assert report.as_dict()["questions"]["cause"]["by_lang"]["es"]["n1"] == 10


def test_one_language_has_no_split():
    qset = s1.parse_set("triage", SPEC)
    examples = [_lang_ex("thermal", "en")] * 4
    report = s1.evaluate(qset, examples, [_answer("thermal", 0.9)] * 4, gates=False)
    assert report.questions[0].by_lang == {}


def test_examples_without_a_language_count_only_in_the_totals():
    qset = s1.parse_set("triage", SPEC)
    examples = [_lang_ex("thermal", "en"), _lang_ex("thermal", "es"), _lang_ex("thermal", "")]
    report = s1.evaluate(qset, examples, [_answer("thermal", 0.9)] * 3, gates=False)
    (q,) = report.questions
    assert set(q.by_lang) == {"en", "es"} and q.metrics.n1 == 3


def test_load_examples_reads_lang(tmp_path):
    qset = _write_set(
        tmp_path,
        examples=[{"state": {"alert": "x"}, "expect": {"cause": "thermal"}, "lang": "ES"}],
    )
    assert s1.load_examples(qset)[0].lang == "es"
    (tmp_path / "triage.eval.jsonl").write_text(
        json.dumps({"state": {}, "expect": {}, "lang": 3}) + "\n"
    )
    with pytest.raises(s1.QuestionSetError, match="lang"):
        s1.load_examples(qset)


def test_answers_must_line_up():
    qset = s1.parse_set("triage", SPEC)
    with pytest.raises(ValueError, match="1 answers for 2 examples"):
        s1.evaluate(qset, [_ex("thermal"), _ex(None)], [_answer("thermal", 0.9)])


# ── Fitting ──────────────────────────────────────────────────────────────────


def test_fit_thresholds_picks_the_lowest_safe_threshold():
    qset = s1.parse_set("triage", SPEC)
    # Right answers are confident; wrong ones and null-example answers sit below 0.6
    examples, answers = [], []
    for i in range(30):
        examples.append(_ex("thermal"))
        answers.append(_answer("thermal", 0.7 + i / 100))
        examples.append(_ex(None))
        answers.append(_answer("electrical", 0.3 + i / 100))
    fit = s1.fit_thresholds(qset, examples, answers)["cause"]
    for on in s1.THRESHOLD_ON:
        t = fit[on]["threshold"]
        assert 0.3 < t <= 0.7
        assert fit[on]["fit"]["confident_wrong"] <= 0.03
        assert set(fit[on]["held_out"]) >= {"coverage", "confident_wrong"}


def test_fit_thresholds_split_is_fixed():
    assert s1._split(30, 0) == s1._split(30, 0)
    fit, held = s1._split(30, 0)
    assert len(fit) == 20 and len(held) == 10 and not set(fit) & set(held)


def test_temp_bucket():
    assert [s1.temp_bucket(k) for k in (2, 3, 5, 6, 10, 11)] == [
        "choice:2",
        "choice:3-5",
        "choice:3-5",
        "choice:6-10",
        "choice:6-10",
        "choice:11+",
    ]


def test_fit_calibration_recovers_a_temperature():
    """Answers drawn from a known model tempered by T=2 should fit back near T=2."""
    qset = s1.parse_set("triage", SPEC)
    examples, answers = [], []
    true = [0.7, 0.2, 0.1]  # calibrated probabilities of (thermal, electrical, unknown)
    shown = s1._retemper(true, 2.0)  # over-confident: what laya would return at T=1
    for i in range(100):
        expected = "thermal" if i < 70 else "electrical" if i < 90 else None
        examples.append(_ex(expected))
        probs = dict(zip(qset.options("cause"), shown, strict=True))
        answers.append(_answer("thermal", 0.9, probabilities=probs))
    # 70 thermal, 20 electrical, 10 null (skipped): the empirical rates are 7/9 and 2/9
    fit = s1.fit_calibration([(qset, examples, answers)])["choice:3-5"]
    assert fit["n"] == 90
    assert fit["nll"] <= fit["nll_at_1"]
    assert 1.2 < fit["temperature"] < 3.0


def test_fit_calibration_undoes_the_site_file():
    qset = s1.parse_set("triage", SPEC)
    probs = dict(zip(qset.options("cause"), [0.6, 0.3, 0.1], strict=True))
    examples = [_ex("thermal")] * 6 + [_ex("electrical")] * 3 + [_ex(None)]
    answers = [_answer("thermal", 0.9, probabilities=probs)] * 10
    a = s1.fit_calibration([(qset, examples, answers)])["choice:3-5"]["temperature"]
    tempered = dict(zip(qset.options("cause"), s1._retemper([0.6, 0.3, 0.1], 1 / 2.0), strict=True))
    answers_t = [_answer("thermal", 0.9, probabilities=tempered)] * 10
    b = s1.fit_calibration([(qset, examples, answers_t)], current={"choice:3-5": 2.0})
    assert b["choice:3-5"]["temperature"] == pytest.approx(a, abs=0.02)


def test_fit_calibration_uses_the_reported_temperature():
    """An answer that says which temperature the service applied is un-tempered by that one."""
    qset = s1.parse_set("triage", SPEC)
    probs = dict(zip(qset.options("cause"), [0.6, 0.3, 0.1], strict=True))
    examples = [_ex("thermal")] * 6 + [_ex("electrical")] * 3
    plain = [_answer("thermal", 0.9, probabilities=probs)] * 9
    a = s1.fit_calibration([(qset, examples, plain)])["choice:3-5"]["temperature"]
    tempered = dict(zip(qset.options("cause"), s1._retemper([0.6, 0.3, 0.1], 1 / 2.0), strict=True))
    reported = [_answer("thermal", 0.9, probabilities=tempered)] * 9
    for r in reported:
        r["cause"] = {**r["cause"], "temperature": 2.0}
    # The site file says 1.5, but the answers say 2.0 was applied: the answers win
    b = s1.fit_calibration([(qset, examples, reported)], current={"choice:3-5": 1.5})
    assert b["choice:3-5"]["temperature"] == pytest.approx(a, abs=0.02)


def test_question_temperatures():
    spec = {**SPEC, "temperatures": {"cause": 1.6}}
    qset = s1.parse_set("triage", spec)
    assert qset.temperatures == {"cause": 1.6}
    examples = [_ex("thermal")] * 7 + [_ex("electrical")] * 2 + [_ex(None)]
    probs = dict(zip(qset.options("cause"), [0.9, 0.08, 0.02], strict=True))
    answers = [_answer("thermal", 0.9, probabilities=probs)] * 10
    fit = s1.fit_question_temperatures(qset, examples, answers)
    assert fit["cause"]["n"] == 9 and fit["cause"]["nll"] <= fit["cause"]["nll_at_1"]
    # A question with its own temperature isn't part of its bucket's fit
    assert s1.fit_calibration([(qset, examples, answers)]) == {}


@pytest.mark.parametrize(
    "temperatures",
    [{"cause": 0.2}, {"cause": 6}, {"cause": True}, {"other": 1.0}, ["cause"]],
)
def test_bad_question_temperatures(temperatures):
    with pytest.raises(s1.QuestionSetError, match="temperatures"):
        s1.parse_set("triage", {**SPEC, "temperatures": temperatures})


def test_retemper():
    p = s1._retemper([0.8, 0.2], 1 / 1.9)
    logit = math.log(0.8 / 0.2)
    assert p[0] == pytest.approx(1 / (1 + math.exp(-logit / 1.9)))
    assert sum(s1._retemper([0.0, 1.0], 2.0)) == pytest.approx(1.0)


# ── Decision log and profiles ────────────────────────────────────────────────


def test_state_sha256_is_canonical():
    assert s1.state_sha256({"b": 1, "a": "é"}) == s1.state_sha256({"a": "é", "b": 1})
    assert len(s1.state_sha256({"alert": "x"})) == 64


def test_decision_payload():
    response = {
        "set": {"name": "incident-triage", "version": 3},
        "state_sha256": "ab" * 32,
        "answers": {
            "cause": {
                "choice": "electrical",
                "confidence": 0.99,
                "abstain": False,
                "probabilities": {},
            }
        },
    }
    d = s1.decision_payload(
        response,
        "n8n-incident-escalation",
        "gw-01",
        "aa8c91ca088ec597df95a0d1c76b3063cb2ae5e8",
        114.6,
        used=True,
    )
    assert d["set"] == "incident-triage" and d["set_version"] == 3
    assert d["revision"] == "aa8c91ca088e" and d["latency_ms"] == 114
    assert d["answers"] == {"cause": {"choice": "electrical", "confidence": 0.99, "abstain": False}}
    assert d["ts"].endswith("Z") and len(d["id"]) == 36
    with pytest.raises(ValueError, match="caller"):
        s1.decision_payload(response, "Bad Caller", "gw-01", "a" * 40, 1, used=False)


def _decision():
    return {
        "id": "2f1c7d9e-6a0b-4d8e-9a51-0c3f5b7e2a14",
        "set": "triage",
        "set_version": 2,
        "caller": "n8n-operator-intake",
        "answers": {"cause": {"choice": "electrical", "confidence": 0.4, "abstain": True}},
    }


def test_feedback_payload():
    f = s1.feedback_payload(_decision(), "cause", "thermal")
    assert f["id"] == _decision()["id"] and f["set_version"] == 2
    assert (f["question"], f["expected"], f["choice"], f["abstain"]) == (
        "cause",
        "thermal",
        "electrical",
        True,
    )
    assert "state" not in f
    assert s1.feedback_payload(_decision(), "cause", "thermal", state={"alert": "x"})["state"]


def test_feedback_examples_merge_by_id():
    qset = s1.parse_set("triage", SPEC)
    two = {**SPEC["questions"]["cause"]}
    qset2 = s1.parse_set("triage", {**SPEC, "questions": {"cause": two, "where": two}})
    msgs = [
        {"id": "a", "set": "triage", "question": "cause", "expected": "thermal", "state": {"t": 1}},
        {
            "id": "a",
            "set": "triage",
            "question": "where",
            "expected": "electrical",
            "state": {"t": 1},
        },
        {"id": "b", "set": "triage", "question": "cause", "expected": "thermal"},  # no state
        {"id": "c", "set": "triage", "question": "cause", "expected": "unknown", "state": {}},
        {"id": "d", "set": "other", "question": "cause", "expected": "thermal", "state": {}},
    ]
    lines = s1.feedback_examples(qset2, msgs)
    assert lines == [
        {
            "state": {"t": 1},
            "expect": {"cause": "thermal", "where": "electrical"},
            "source": "feedback",
            "id": "a",
        }
    ]
    assert [x["expect"] for x in s1.feedback_examples(qset, msgs)] == [{"cause": "thermal"}]
    msgs[0]["lang"] = "ES"
    assert s1.feedback_examples(qset, msgs)[0]["lang"] == "es"


def test_profiles():
    assert s1.add_profile("ollama,n8n") == "ollama,n8n,system1"
    assert s1.add_profile("ollama, system1") == "ollama,system1"
    assert s1.add_profile("") == "system1"
    assert s1.remove_profile("ollama,system1,n8n") == "ollama,n8n"
    assert s1.remove_profile("system1") == ""


def test_shipped_sets_are_valid():
    """The ai stack's sets and evaluation files, when the monorepo has them next to the lib."""
    folder = Path(__file__).resolve().parents[2] / "stacks" / "ai" / s1.SETS_DIR
    if not folder.is_dir():
        pytest.skip("ai stack not checked out next to the lib")
    sets = s1.list_sets(folder)
    assert {"incident-triage", "operator-intake"} <= {q.name for q in sets}
    for qset in sets:
        assert s1.size_failures(qset, s1.load_examples(qset)) == [], qset.name
