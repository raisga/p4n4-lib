"""System 1 (laya typed decisions): question sets, evaluation, calibration and the decision log.

Shared by the CLI (`p4n4 ai system1`) and the harness. Nothing here talks to the service: callers
send the requests (see CLIENT) and hand the answers in, so every metric is a pure function.
The design and the rules these checks enforce are in p4n4-docs, decisions/system1.md.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Where a project's ai layer keeps its sets, and the Compose profile and key that enable it
SETS_DIR = "config/system1/questions"
PROFILE = "system1"
API_KEY = "SYSTEM1_API_KEY"
# What channel adapters send to n8n's operator-intake webhook
INTAKE_TOKEN = "OPERATOR_INTAKE_TOKEN"
# What Grafana's drift alerts send to n8n's system1-drift webhook (iot and ai layers)
ALERT_TOKEN = "SYSTEM1_ALERT_TOKEN"
# U7: labels of past events, as points Grafana shows as annotations
LABEL_MEASUREMENT = "system1_label"
SERVICE = "system1"
PULL_SERVICE = "system1-pull"
MIN_API_KEY_LENGTH = 32

DEFAULT_MIN_EXAMPLES = 50
# Per option key, and of `null` ("no answer is right") examples, before gates mean anything
MIN_PER_OPTION = 5
MIN_NULL = 10
DEFAULT_GATES = {"min_coverage": 0.6, "min_answered_accuracy": 0.9, "max_confident_wrong": 0.03}
THRESHOLD_ON = ("confidence", "answer_confidence")
SET_KEYS = {
    "version",
    "description",
    "questions",
    "thresholds",
    "threshold_on",
    "temperatures",
    "eval",
}
EVAL_KEYS = {"min_examples", "no_answer_keys", "gates", "rank"}
# A ranking question (eval.rank) is a signal to sort by, never a gate on its own answer: it is
# judged by how well the probability of its positive option ranks the examples (system1.md U6)
RANK_GATES = {"min_auc": 0.8}

# laya's temperature buckets (laya.common.temp_bucket) and its clamp range
TEMP_MIN, TEMP_MAX = 0.5, 5.0
# laya rounds probabilities to 4 decimals, so a 0 is anything under 5e-5
PROB_FLOOR = 5e-5

# Decision-log tags must stay within the menus, or series cardinality grows with free text
TAG_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
DECISIONS_TOPIC = "system1/decisions/{set}"
FEEDBACK_TOPIC = "system1/feedback/{set}"


class QuestionSetError(ValueError):
    """A question set or an evaluation file that can't be used."""


@dataclass(frozen=True)
class QuestionSet:
    name: str
    version: int
    questions: dict[str, dict]
    thresholds: dict[str, float]
    threshold_on: str = "confidence"
    # Per-question temperatures the service applies in place of laya's bucket temperature
    temperatures: dict[str, float] = field(default_factory=dict)
    eval: dict = field(default_factory=dict)
    description: str = ""
    path: Path | None = None

    def options(self, question: str) -> list[str]:
        criteria = self.questions[question]["criteria"]
        return list(criteria) if isinstance(criteria, dict) else list(criteria)

    def no_answer_keys(self, question: str) -> set[str]:
        return set((self.eval.get("no_answer_keys") or {}).get(question, ()))

    def rank_positive(self, question: str) -> str | None:
        """The option a ranking question sorts by, or None for an ordinary question."""
        return (self.eval.get("rank") or {}).get(question)

    def gates(self, question: str) -> dict[str, float]:
        base = RANK_GATES if self.rank_positive(question) else DEFAULT_GATES
        return {**base, **((self.eval.get("gates") or {}).get(question) or {})}

    @property
    def min_examples(self) -> int:
        return int(self.eval.get("min_examples", DEFAULT_MIN_EXAMPLES))

    @property
    def eval_path(self) -> Path | None:
        return self.path.with_name(f"{self.name}.eval.jsonl") if self.path else None


@dataclass(frozen=True)
class Example:
    state: Any
    expect: dict[str, str | None]
    source: str
    line: int
    lang: str = ""


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def parse_set(name: str, spec: Any, path: Path | None = None) -> QuestionSet:
    """Check a set's shape the way the service does at start-up, minus the token budgets (those
    need the tokenizer, so only the service checks them)."""
    where = f"question set {name}"
    if not isinstance(spec, dict):
        raise QuestionSetError(f"{where}: must be a JSON object")
    unknown = sorted(set(spec) - SET_KEYS)
    if unknown:
        raise QuestionSetError(f"{where}: unknown keys {unknown}")
    version = spec.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise QuestionSetError(f"{where}: 'version' must be a positive integer")
    questions = spec.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise QuestionSetError(f"{where}: needs a non-empty 'questions' object")
    for qid, q in questions.items():
        if not isinstance(q, dict) or q.get("type") != "choice":
            raise QuestionSetError(f"{where}: question {qid!r} must be of type 'choice'")
        if not isinstance(q.get("instructions"), str) or not q["instructions"].strip():
            raise QuestionSetError(f"{where}: question {qid!r} needs 'instructions'")
        criteria = q.get("criteria")
        if not isinstance(criteria, dict | list) or len(criteria) < 2:
            raise QuestionSetError(f"{where}: question {qid!r} needs at least 2 'criteria'")
    thresholds = spec.get("thresholds", {})
    if not isinstance(thresholds, dict):
        raise QuestionSetError(f"{where}: 'thresholds' must be an object")
    bad = sorted(
        k
        for k, v in thresholds.items()
        if k not in questions or not _is_number(v) or not 0 <= v <= 1
    )
    if bad:
        raise QuestionSetError(f"{where}: thresholds {bad} must name a question and lie in [0, 1]")
    threshold_on = spec.get("threshold_on", "confidence")
    if threshold_on not in THRESHOLD_ON:
        raise QuestionSetError(f"{where}: 'threshold_on' must be one of {list(THRESHOLD_ON)}")
    temperatures = spec.get("temperatures", {})
    if not isinstance(temperatures, dict):
        raise QuestionSetError(f"{where}: 'temperatures' must be an object")
    bad = sorted(
        k
        for k, v in temperatures.items()
        if k not in questions or not _is_number(v) or not TEMP_MIN <= v <= TEMP_MAX
    )
    if bad:
        raise QuestionSetError(
            f"{where}: temperatures {bad} must name a question and lie in "
            f"[{TEMP_MIN:g}, {TEMP_MAX:g}]"
        )
    ev = spec.get("eval", {})
    if not isinstance(ev, dict) or set(ev) - EVAL_KEYS:
        raise QuestionSetError(f"{where}: 'eval' takes only {sorted(EVAL_KEYS)}")
    qset = QuestionSet(
        name=name,
        version=version,
        questions=questions,
        thresholds={k: float(v) for k, v in thresholds.items()},
        threshold_on=threshold_on,
        temperatures={k: float(v) for k, v in temperatures.items()},
        eval=ev,
        description=spec.get("description", ""),
        path=path,
    )
    for qid, keys in (ev.get("no_answer_keys") or {}).items():
        if qid not in questions or not set(keys) <= set(qset.options(qid)):
            raise QuestionSetError(f"{where}: no_answer_keys for {qid!r} must name its options")
    rank = ev.get("rank") or {}
    if not isinstance(rank, dict) or any(
        q not in questions or k not in qset.options(q) for q, k in rank.items()
    ):
        raise QuestionSetError(f"{where}: rank must map questions to one of their options")
    for qid, gates in (ev.get("gates") or {}).items():
        allowed = RANK_GATES if qid in rank else DEFAULT_GATES
        if qid not in questions or not isinstance(gates, dict) or set(gates) - set(allowed):
            raise QuestionSetError(
                f"{where}: gates for {qid!r} must name a question and use {sorted(allowed)}"
            )
        if not all(_is_number(v) and 0 <= v <= 1 for v in gates.values()):
            raise QuestionSetError(f"{where}: gates for {qid!r} must lie in [0, 1]")
    return qset


def load_set(path: Path) -> QuestionSet:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QuestionSetError(f"question set {path.stem}: {exc}") from exc
    return parse_set(path.stem, spec, path)


def list_sets(folder: Path) -> list[QuestionSet]:
    """Every <name>.json set in `folder`, by name. Evaluation files (.eval.jsonl) are skipped."""
    if not folder.is_dir():
        return []
    return [load_set(p) for p in sorted(folder.glob("*.json"))]


def load_examples(qset: QuestionSet, path: Path | None = None) -> list[Example]:
    """The set's evaluation examples, one JSON object per line (blank lines are skipped). An
    optional `lang` (e.g. "es") splits the metrics, and the gates, by language."""
    path = path or qset.eval_path
    if path is None or not path.is_file():
        raise QuestionSetError(f"question set {qset.name}: no evaluation file at {path}")
    examples = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        where = f"{path.name}:{n}"
        try:
            obj = json.loads(raw)
        except ValueError as exc:
            raise QuestionSetError(f"{where}: {exc}") from exc
        if not (isinstance(obj, dict) and "state" in obj and isinstance(obj.get("expect"), dict)):
            raise QuestionSetError(f"{where}: needs 'state' and an 'expect' object")
        for qid, key in obj["expect"].items():
            if qid not in qset.questions:
                raise QuestionSetError(f"{where}: {qid!r} is not a question of {qset.name}")
            if key is not None and key not in qset.options(qid):
                raise QuestionSetError(f"{where}: {key!r} is not an option of {qid!r}")
            if key in qset.no_answer_keys(qid):
                raise QuestionSetError(f"{where}: expect null, not the no-answer key {key!r}")
        lang = obj.get("lang", "")
        if not isinstance(lang, str):
            raise QuestionSetError(f"{where}: 'lang' must be a language code such as \"es\"")
        examples.append(
            Example(obj["state"], obj["expect"], str(obj.get("source", "")), n, lang.lower())
        )
    return examples


# ── Metrics and gates ────────────────────────────────────────────────────────


def _answered(answer: dict, no_answer: set[str], threshold: float | None, on: str) -> bool:
    """Whether a consumer would use this answer. With `threshold` None, the service's own
    `abstain` flag decides; otherwise it is recomputed (for threshold fitting)."""
    if answer.get("choice") in no_answer:
        return False
    if threshold is None:
        return not answer.get("abstain", False)
    return answer.get(on, 0.0) >= threshold


@dataclass
class Metrics:
    n1: int = 0  # examples with an expected key
    n0: int = 0  # examples where no answer is right
    answered: int = 0  # answered n1 examples
    right: int = 0
    answered_null: int = 0  # n0 examples that got an answer anyway

    @property
    def coverage(self) -> float:
        return self.answered / self.n1 if self.n1 else 0.0

    @property
    def answered_accuracy(self) -> float:
        return self.right / self.answered if self.answered else 0.0

    @property
    def confident_wrong(self) -> float:
        total = self.n1 + self.n0
        return ((self.answered - self.right) + self.answered_null) / total if total else 0.0

    @property
    def null_abstention(self) -> float:
        return 1 - self.answered_null / self.n0 if self.n0 else 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "n1": self.n1,
            "n0": self.n0,
            "coverage": round(self.coverage, 4),
            "answered_accuracy": round(self.answered_accuracy, 4),
            "confident_wrong": round(self.confident_wrong, 4),
            "null_abstention": round(self.null_abstention, 4),
        }


def _metrics(
    qset: QuestionSet,
    qid: str,
    pairs: list[tuple[Example, dict]],
    threshold: float | None = None,
    on: str | None = None,
) -> Metrics:
    m = Metrics()
    no_answer = qset.no_answer_keys(qid)
    on = on or qset.threshold_on
    for ex, answers in pairs:
        if qid not in ex.expect:
            continue
        expected, answer = ex.expect[qid], answers[qid]
        used = _answered(answer, no_answer, threshold, on)
        if expected is None:
            m.n0 += 1
            m.answered_null += used
        else:
            m.n1 += 1
            m.answered += used
            m.right += used and answer.get("choice") == expected
    return m


@dataclass
class QuestionReport:
    question: str
    metrics: Metrics
    gates: dict[str, float]
    by_source: dict[str, Metrics]
    confusion: dict[str, dict[str, int]]  # expected (or "null") -> answer (or "abstain") -> n
    failures: list[str]
    # Only when the examples carry two or more languages: the gates then hold in each one
    by_lang: dict[str, Metrics] = field(default_factory=dict)
    # Ranking questions only: AUC of the positive option's probability, overall and per language
    auc: float | None = None
    auc_by_lang: dict[str, float] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass
class Report:
    set: str
    version: int
    examples: int
    questions: list[QuestionReport]
    latency_ms: dict[str, float]
    failures: list[str]  # gate and size failures, every question

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        return {
            "set": self.set,
            "version": self.version,
            "examples": self.examples,
            "passed": self.passed,
            "failures": self.failures,
            "latency_ms": self.latency_ms,
            "questions": {
                q.question: {
                    **q.metrics.as_dict(),
                    "gates": q.gates,
                    "by_source": {s: m.as_dict() for s, m in q.by_source.items()},
                    "by_lang": {lang: m.as_dict() for lang, m in q.by_lang.items()},
                    "confusion": q.confusion,
                    **({"auc": q.auc, "auc_by_lang": q.auc_by_lang} if q.auc is not None else {}),
                }
                for q in self.questions
            },
        }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(pct / 100 * len(ordered)) - 1))]


def size_failures(qset: QuestionSet, examples: list[Example]) -> list[str]:
    """Why the examples are too few for the gates to mean anything; empty when they suffice."""
    out = []
    if len(examples) < qset.min_examples:
        out.append(f"{len(examples)} examples, the set asks for at least {qset.min_examples}")
    for qid in qset.questions:
        scored = [ex.expect[qid] for ex in examples if qid in ex.expect]
        if not scored:
            continue
        no_answer = qset.no_answer_keys(qid)
        short = [
            k
            for k in qset.options(qid)
            if k not in no_answer and sum(e == k for e in scored) < MIN_PER_OPTION
        ]
        if short:
            out.append(f"{qid}: fewer than {MIN_PER_OPTION} examples for {', '.join(short)}")
        if qset.rank_positive(qid):
            continue  # ranked, never answered: no null examples to abstain on
        nulls = sum(e is None for e in scored)
        if nulls < MIN_NULL:
            out.append(f"{qid}: {nulls} null examples, at least {MIN_NULL} are needed")
    return out


def _gate_failures(label: str, m: Metrics, gates: dict[str, float]) -> list[str]:
    out = []
    if m.coverage < gates["min_coverage"]:
        out.append(f"{label}: coverage {m.coverage:.2f} < {gates['min_coverage']:.2f}")
    if m.answered_accuracy < gates["min_answered_accuracy"]:
        out.append(
            f"{label}: answered accuracy {m.answered_accuracy:.2f} < "
            f"{gates['min_answered_accuracy']:.2f}"
        )
    if m.confident_wrong > gates["max_confident_wrong"]:
        out.append(
            f"{label}: confident-wrong rate {m.confident_wrong:.3f} > "
            f"{gates['max_confident_wrong']:.3f}"
        )
    return out


def auc(scores: list[tuple[float, bool]]) -> float | None:
    """Probability that a positive example scores above a negative one (ties count half);
    None without both."""
    pos = [s for s, p in scores if p]
    neg = [s for s, p in scores if not p]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 4)


def _rank_scores(qid: str, positive: str, pairs: list[tuple[Example, dict]]):
    return [
        (a[qid]["probabilities"].get(positive, 0.0), ex.expect[qid] == positive)
        for ex, a in pairs
        if ex.expect.get(qid) is not None and qid in a
    ]


def evaluate(
    qset: QuestionSet,
    examples: list[Example],
    answers: list[dict[str, dict]],
    latencies_ms: list[float] | None = None,
    gates: bool = True,
) -> Report:
    """Score the service's answers (each response's `answers`, in example order) against the
    examples. Answered means not abstained and not a no-answer key (system1.md §10.2). When the
    examples carry two or more languages, each question's gates must hold in every one of them."""
    if len(answers) != len(examples):
        raise ValueError(f"{len(answers)} answers for {len(examples)} examples")
    pairs = list(zip(examples, answers, strict=True))
    failures = size_failures(qset, examples) if gates else []
    reports = []
    for qid in qset.questions:
        m = _metrics(qset, qid, pairs)
        if not m.n1 and not m.n0:
            continue
        sources = sorted({ex.source for ex, _ in pairs if qid in ex.expect})
        by_source = {
            s: _metrics(qset, qid, [(ex, a) for ex, a in pairs if ex.source == s]) for s in sources
        }
        confusion: dict[str, dict[str, int]] = {}
        no_answer = qset.no_answer_keys(qid)
        for ex, a in pairs:
            if qid not in ex.expect:
                continue
            row = confusion.setdefault(ex.expect[qid] or "null", {})
            got = a[qid]["choice"] if _answered(a[qid], no_answer, None, "") else "abstain"
            row[got] = row.get(got, 0) + 1
        # Examples without a language count in the totals but in no language
        langs = sorted({ex.lang for ex, _ in pairs if qid in ex.expect} - {""})
        by_lang = (
            {
                lang: _metrics(qset, qid, [(ex, a) for ex, a in pairs if ex.lang == lang])
                for lang in langs
            }
            if len(langs) > 1
            else {}
        )
        q_gates = qset.gates(qid)
        q_failures = []
        positive = qset.rank_positive(qid)
        q_auc, auc_by_lang = None, {}
        if positive:
            q_auc = auc(_rank_scores(qid, positive, pairs))
            if by_lang:
                for lang in by_lang:
                    lang_pairs = [(ex, a) for ex, a in pairs if ex.lang == lang]
                    auc_by_lang[lang] = auc(_rank_scores(qid, positive, lang_pairs))
            if gates:
                for label, value in [(qid, q_auc)] + [
                    (f"{qid} [{lang}]", v) for lang, v in auc_by_lang.items()
                ]:
                    if value is None or value < q_gates["min_auc"]:
                        shown = "n/a" if value is None else f"{value:.2f}"
                        q_failures.append(f"{label}: AUC {shown} < {q_gates['min_auc']:.2f}")
        elif gates:
            q_failures = _gate_failures(qid, m, q_gates)
            for lang, lm in by_lang.items():
                q_failures += _gate_failures(f"{qid} [{lang}]", lm, q_gates)
        failures.extend(q_failures)
        reports.append(
            QuestionReport(
                qid, m, q_gates, by_source, confusion, q_failures, by_lang, q_auc, auc_by_lang
            )
        )
    lat = latencies_ms or []
    return Report(
        set=qset.name,
        version=qset.version,
        examples=len(examples),
        questions=reports,
        latency_ms={"p50": _percentile(lat, 50), "p95": _percentile(lat, 95)},
        failures=failures,
    )


# ── Threshold fitting ────────────────────────────────────────────────────────


def _split(n: int, seed: int) -> tuple[list[int], list[int]]:
    """Two thirds to fit on, one third held out, the same every run."""
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    cut = (2 * n) // 3
    return sorted(idx[:cut]), sorted(idx[cut:])


def fit_thresholds(
    qset: QuestionSet,
    examples: list[Example],
    answers: list[dict[str, dict]],
    seed: int = 0,
) -> dict[str, dict[str, dict]]:
    """For each question and each confidence metric, the lowest threshold that meets the set's
    confident-wrong and answered-accuracy gates on two thirds of the examples, with the gates'
    metrics on the held-out third. Nothing is written: a person edits the set and bumps it."""
    fit_idx, held_idx = _split(len(examples), seed)
    pairs = list(zip(examples, answers, strict=True))
    fit = [pairs[i] for i in fit_idx]
    held = [pairs[i] for i in held_idx]
    out: dict[str, dict[str, dict]] = {}
    for qid in qset.questions:
        if qset.rank_positive(qid):
            continue  # ranked by probability, never thresholded
        gates = qset.gates(qid)
        out[qid] = {}
        for on in THRESHOLD_ON:
            values = {a[qid].get(on) for _, a in fit if qid in a and a[qid].get(on) is not None}
            if not values:
                continue
            chosen = None
            for t in sorted(values | {0.0, 1.0}):
                m = _metrics(qset, qid, fit, threshold=t, on=on)
                accurate = m.answered_accuracy >= gates["min_answered_accuracy"] or not m.answered
                if m.confident_wrong <= gates["max_confident_wrong"] and accurate:
                    chosen = t
                    break
            if chosen is None:
                out[qid][on] = {"threshold": None}
                continue
            out[qid][on] = {
                "threshold": round(chosen, 4),
                "fit": _metrics(qset, qid, fit, threshold=chosen, on=on).as_dict(),
                "held_out": _metrics(qset, qid, held, threshold=chosen, on=on).as_dict(),
            }
    return out


# ── Calibration ──────────────────────────────────────────────────────────────


def temp_bucket(k: int, qtype: str = "choice") -> str:
    """laya's temperature bucket for a question with `k` options."""
    size = "2" if k <= 2 else "3-5" if k <= 5 else "6-10" if k <= 10 else "11+"
    return f"{qtype}:{size}"


def _retemper(probs: list[float], power: float) -> list[float]:
    """p_i^power, renormalised: undoes (power = T) or applies (power = 1/T) a temperature."""
    logs = [power * math.log(max(p, PROB_FLOOR)) for p in probs]
    top = max(logs)
    exps = [math.exp(v - top) for v in logs]
    total = sum(exps)
    return [e / total for e in exps]


def _raw_rows(
    qset: QuestionSet,
    qid: str,
    pairs: list[tuple[Example, dict]],
    current: dict[str, float],
) -> list[tuple[list[float], int]]:
    """(uncalibrated probabilities, expected index) for each keyed example of a question. Each
    answer is un-tempered by the temperature the service reports it applied, else by `current`."""
    options = qset.options(qid)
    bucket = temp_bucket(len(options))
    rows = []
    for ex, a in pairs:
        expected = ex.expect.get(qid)
        if expected is None or qid not in a:
            continue
        applied = a[qid].get("temperature", current.get(bucket, 1.0))
        probs = [a[qid]["probabilities"].get(k, 0.0) for k in options]
        rows.append((_retemper(probs, applied), options.index(expected)))
    return rows


def _fit_temperature(data: list[tuple[list[float], int]], step: float) -> dict[str, float]:
    """The temperature in [TEMP_MIN, TEMP_MAX] minimising the mean negative log-likelihood."""
    best_t, best_nll = 1.0, math.inf
    for i in range(round((TEMP_MAX - TEMP_MIN) / step) + 1):
        t = TEMP_MIN + i * step
        nll = -sum(math.log(max(_retemper(p, 1 / t)[y], PROB_FLOOR)) for p, y in data)
        if nll < best_nll:
            best_t, best_nll = t, nll
    base = -sum(math.log(max(p[y], PROB_FLOOR)) for p, y in data)
    return {
        "temperature": round(best_t, 2),
        "n": len(data),
        "nll": round(best_nll / len(data), 4),
        "nll_at_1": round(base / len(data), 4),
    }


def fit_calibration(
    sets: list[tuple[QuestionSet, list[Example], list[dict[str, dict]]]],
    current: dict[str, float] | None = None,
    step: float = 0.01,
) -> dict[str, dict[str, float]]:
    """One temperature per laya bucket, minimising the negative log-likelihood of the expected
    keys on the non-null examples of every set given (buckets are shared across sets). Questions
    with a temperature of their own in their set are left out: the bucket doesn't apply to them.
    Returned probabilities are first un-tempered (see _raw_rows)."""
    current = current or {}
    rows: dict[str, list[tuple[list[float], int]]] = {}
    for qset, examples, answers in sets:
        pairs = list(zip(examples, answers, strict=True))
        for qid in qset.questions:
            if qid in qset.temperatures:
                continue
            bucket = temp_bucket(len(qset.options(qid)))
            rows.setdefault(bucket, []).extend(_raw_rows(qset, qid, pairs, current))
    return {bucket: _fit_temperature(data, step) for bucket, data in sorted(rows.items()) if data}


def fit_question_temperatures(
    qset: QuestionSet,
    examples: list[Example],
    answers: list[dict[str, dict]],
    current: dict[str, float] | None = None,
    step: float = 0.01,
) -> dict[str, dict[str, float]]:
    """One temperature per question of the set, for its `temperatures` key: the alternative to a
    bucket temperature when one question needs a different correction than the others sharing
    its bucket. Nothing is written: a person edits the set and bumps its version."""
    pairs = list(zip(examples, answers, strict=True))
    out = {}
    for qid in qset.questions:
        data = _raw_rows(qset, qid, pairs, current or {})
        if data:
            out[qid] = _fit_temperature(data, step)
    return out


# ── Feedback ─────────────────────────────────────────────────────────────────


def feedback_payload(
    decision: dict,
    question: str,
    expected: str,
    state: Any = None,
    caller: str | None = None,
    lang: str | None = None,
) -> dict:
    """The message a caller publishes on system1/feedback/<set> when a person confirms or corrects
    one answer of a logged decision. `state` is included only when the site opts in to keeping
    examples (it can hold personal data); without it the feedback still feeds the dashboard."""
    answer = decision["answers"][question]
    out = {
        "id": decision["id"],
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "set": decision["set"],
        "set_version": decision["set_version"],
        "caller": caller or decision["caller"],
        "question": question,
        "expected": expected,
        "choice": answer["choice"],
        "abstain": answer["abstain"],
    }
    if lang:
        out["lang"] = lang
    if state is not None:
        out["state"] = state
    return out


def feedback_examples(qset: QuestionSet, messages: list[dict]) -> list[dict]:
    """Evaluation lines (source "feedback") from feedback messages that carry their state, one
    per decision id with every question's confirmed key. A key the set doesn't offer, or a
    no-answer key, is dropped; messages without a state are skipped."""
    merged: dict[str, dict] = {}
    for msg in messages:
        if (
            msg.get("set") != qset.name
            or "state" not in msg
            or msg.get("question") not in qset.questions
        ):
            continue
        qid, expected = msg["question"], msg.get("expected")
        if expected not in qset.options(qid) or expected in qset.no_answer_keys(qid):
            continue
        line = merged.setdefault(
            msg["id"], {"state": msg["state"], "expect": {}, "source": "feedback", "id": msg["id"]}
        )
        line["expect"][qid] = expected
        if isinstance(msg.get("lang"), str) and msg["lang"]:
            line["lang"] = msg["lang"].lower()
    return list(merged.values())


# ── Decision log ─────────────────────────────────────────────────────────────


def state_sha256(state: Any) -> str:
    """sha256 of the state as compact JSON with sorted keys, as the service reports it."""
    text = json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def decision_payload(
    response: dict,
    caller: str,
    subject: str,
    revision: str,
    latency_ms: int,
    used: bool,
    checkpoint: str = "multilingual",
) -> dict:
    """The message a caller publishes on system1/decisions/<set> for one set response (§6.7)."""
    if not TAG_RE.match(caller):
        raise ValueError(f"caller {caller!r} must match {TAG_RE.pattern}")
    return {
        "id": str(uuid.uuid4()),
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "set": response["set"]["name"],
        "set_version": response["set"]["version"],
        "checkpoint": checkpoint,
        "revision": revision[:12],
        "caller": caller,
        "subject": subject,
        "state_sha256": response.get("state_sha256", ""),
        "latency_ms": int(latency_ms),
        "answers": {
            qid: {"choice": a["choice"], "confidence": a["confidence"], "abstain": a["abstain"]}
            for qid, a in response["answers"].items()
        },
        "used": bool(used),
    }


# ── Labels for past events (U7) ──────────────────────────────────────────────


def _escape_field(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def label_lines(
    qset: QuestionSet,
    rows: list[dict],
    responses: list[dict],
    caller: str = "cli-label",
) -> list[str]:
    """InfluxDB line protocol for the answered labels of past events: one `system1_label` point
    per answered question, at the event's own time (`ts`, ISO 8601). Abstained answers and
    no-answer keys are left out, so a missing label means "System 1 couldn't tell"."""
    if not TAG_RE.match(caller):
        raise ValueError(f"caller {caller!r} must match {TAG_RE.pattern}")
    out = []
    for row, response in zip(rows, responses, strict=True):
        ts = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
        ms = int(ts.timestamp() * 1000)
        for qid, a in response["answers"].items():
            if a.get("abstain") or a["choice"] in qset.no_answer_keys(qid):
                continue
            tags = {"caller": caller, "choice": a["choice"], "question": qid, "set": qset.name}
            if not all(TAG_RE.match(str(v)) for v in tags.values()):
                continue
            fields = [
                f"confidence={float(a['confidence'])}",
                f"set_version={int(response['set']['version'])}i",
                f"subject={_escape_field(row.get('subject', ''))}",
                f"state_sha256={_escape_field(response.get('state_sha256', ''))}",
            ]
            tag_text = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
            out.append(f"{LABEL_MEASUREMENT},{tag_text} {','.join(fields)} {ms}")
    return out


# ── COMPOSE_PROFILES ─────────────────────────────────────────────────────────


def parse_profiles(value: str) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def add_profile(value: str, name: str = PROFILE) -> str:
    """COMPOSE_PROFILES with `name` added at the end, other entries kept in their order."""
    profiles = parse_profiles(value)
    return ",".join(profiles if name in profiles else [*profiles, name])


def remove_profile(value: str, name: str = PROFILE) -> str:
    return ",".join(p for p in parse_profiles(value) if p != name)


# ── In-container client ──────────────────────────────────────────────────────

# Run inside the system1 container (`docker compose exec -T system1 python -c CLIENT <set>`), so
# the key never leaves it and no host port is needed. Reads one JSON state per stdin line, posts
# each to the set (or, with set "-", one /v1/systemone request per line) and writes one JSON line
# per request: {"status", "latency_ms", "body"}. With set "=sets" it makes one GET /v1/sets.
LIST_SETS = "=sets"
CLIENT = r"""
import json, os, sys, time, urllib.error, urllib.request
name = sys.argv[1]
url = "http://127.0.0.1:8000/" + ("v1/systemone" if name == "-" else "v1/sets" if name == "=sets"
                                  else "v1/sets/" + name)
key = os.environ["LAYA_API_KEY"]
for line in ["GET"] if name == "=sets" else sys.stdin:
    if not line.strip():
        continue
    body = (None if name == "=sets" else line.encode() if name == "-"
            else json.dumps({"state": json.loads(line)}).encode())
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key})
    t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, out = r.status, json.load(r)
    except urllib.error.HTTPError as e:
        status, out = e.code, json.loads(e.read() or b"{}")
    except OSError as e:
        status, out = 0, {"detail": str(e)}
    ms = round((time.monotonic() - t) * 1000, 1)
    print(json.dumps({"status": status, "latency_ms": ms, "body": out}), flush=True)
"""
