#!/usr/bin/env python3
"""Study B of docs/JUDGMENT.md: does a calibrated model classify criterion shape
better than a keyword rule?

docs/CRITERIA.md records that a lexicon refusing open-set criteria was rejected
because it fired on 36 of the 128 criteria in this repository's backlog,
including three tickets that are done. That is a precision complaint, and this
script decides whether a System One model answers it.

It runs before any module lands in `src/anvil/`, so it carries its own client
and its own copy of the question set. The questions must stay identical to
JUDGMENT.md section 7.2; when that section changes, change this file in the same
commit, because a threshold calibrated here is only meaningful for the questions
that produced it.

## The baseline is a family, not a rule

The rejected lexicon's word list was never recorded, only its result. Searching
subsets of the obvious vocabulary finds 109 different lexicons that fire on
exactly 36 of the 128, and neither natural reading of rule 3 lands there: an
absence lexicon fires on 49, a universal-quantifier lexicon on 18. The original
is not recoverable.

So this does not guess it. It searches the whole family and reports the *best
achievable* keyword rule against the same hand labels -- the strongest baseline
any word list could have reached. Beating a tuned upper bound is a real result;
beating one guess would not be.

## Result

Run 2026-09-18 over 138 hand-labeled criteria, three passes of 138 calls, $0.012
total. Rules 4 and 6 beat the best keyword rule that could be written (0.83 and
0.88 against 0.66 and 0.18, rule 6 at precision 1.00). Rule 1 never fired.

Rule 3 is unresolved. It first measured as a loss, then as a win once a labeling
pass corrected inconsistent labels -- and every point of that improvement came
from corrections made after seeing the model's answers. docs/JUDGMENT.md section
11.5 carries the sensitivity analysis that establishes this and what a blind
pass would have to look like. Do not cite a rule 3 number from this script's
output without reading it.

Two findings worth repeating here, because they are about this file rather than
about Anvil.

Rule 6 asked as one question scored F1 0.00, and the same model on the same
criteria scored 0.88 once the question was split in two. Nothing errored in
between. A question that is wrong returns confident, well-formed answers, so no
application of this API is trustworthy before it has a labeled set to check
against.

And a labeling pass run after seeing model output cannot settle a close
question, however carefully it disagrees. Counting how often the reviewer
disagreed back does not detect the bias; reverting the corrections in groups and
watching where the margin moves does. Section 11.5 has the table.

## Order of operations

    tools/study-b.py extract                 # write the labeling template
    $EDITOR study-b/labels.jsonl             # a human fills in the labels
    tools/study-b.py baseline                # best keyword rule, no API needed
    tools/study-b.py ask --dry-run           # what the calls would cost
    tools/study-b.py ask                     # call the model; results cached
    tools/study-b.py report                  # precision, recall, verdict

`extract`, `baseline` and `report` make no network call. `ask` caches every
response keyed by criterion text, question set, and model, so re-running costs
nothing and a later threshold is re-derivable from the cache without re-calling.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "study-b"
LABELS = OUT / "labels.jsonl"
CACHE = OUT / "responses.json"

MODEL = "jev-1.13.0"
ENDPOINT = os.environ.get("TYPESAFE_ENDPOINT", "https://api.typesafe.ai/v1/systemone")
KEY_VARIABLE = "TYPESAFE_API_KEY"

#: The corpus CRITERIA.md measured the rejected lexicon against. Reported
#: separately from the rest so the comparison is like for like.
MEASURED_FILE = "delivery-dashboard.json"


# --- The question set (JUDGMENT.md section 7.2) ---------------------------

PROCEDURE = {
    "execution": "Settled by running a command, a test suite, or the program, and reading what it reports",
    "reading": "Settled by reading the changed source code",
    "artifact": "Settled by checking that a named file exists or contains particular text",
    "none": "Cannot be settled by running something, by reading the changes, or by checking a file",
}

QUESTIONS = {
    "procedure": {
        "type": "choice",
        "instructions": "Read `criterion.text`. Which procedure would settle whether that statement is true?",
        "criteria": PROCEDURE,
    },
    "unbounded_absence": {
        "type": "noul",
        "instructions": "Does `criterion.text` require that something is absent across a set of places, without listing those places?",
        "criteria": {
            "true": "It asserts an absence over a set it does not enumerate",
            "false": "It names the places it holds over, or it asserts no absence at all",
        },
    },
    "joins_claims": {
        "type": "noul",
        "instructions": "Does `criterion.text` state two separate requirements that could be true independently of each other?",
        "criteria": {
            "true": "Two or more requirements, either of which could hold while the other fails",
            "false": "One requirement",
        },
    },
    # Rule 6 was one question asking whether the author could satisfy the
    # criterion with their own test. The model read it as "could a test fake
    # this behavior", ranked the clear cases below the unclear ones, and scored
    # F1 0.00. It is two hops: what the criterion is about, and whose tests
    # those are. Asked as two literal questions and combined in code, per the
    # decomposition rule in section 4.3.
    # Rule 3 as one question scored F1 0.34 against a keyword rule's 0.67, with a
    # mean noul of 0.45 on the positives -- the model reporting genuine
    # uncertainty rather than a wrong answer. It has the same two-hop shape rule
    # 6 had: whether an absence is asserted, and whether the places are listed.
    # Split, with structured criteria carrying examples. The examples are
    # invented rather than drawn from the corpus, so no scored item appears in
    # its own question.
    "asserts_absence": {
        "type": "noul",
        "instructions": {
            "rule": "An acceptance criterion may require that something is absent: that it never happens, does not exist, is not present, or that only certain listed things occur.",
            "question": "Does `criterion.text` require that something is absent?",
        },
        "criteria": {
            "true": {
                "meaning": "Something must not happen, must not exist, or must be excluded",
                "examples": [
                    "the cache is never written by a background thread",
                    "no endpoint modifies stored data",
                    "only the listed fields are serialized",
                ],
            },
            "false": {
                "meaning": "Everything it requires is something that must be present, produced, or true",
                "examples": [
                    "the parser accepts UTF-8 input",
                    "the report includes a per-item total",
                ],
            },
        },
    },
    "names_the_places": {
        "type": "noul",
        "instructions": {
            "rule": "A statement about where something holds can only be settled if the places are listed. Named files, functions, commands or components count as listed; an open-ended scope does not.",
            "question": "Does `criterion.text` list the places, files, call sites, commands, or components it holds over?",
        },
        "criteria": {
            "true": {
                "meaning": "The places are enumerated in the text itself",
                "examples": [
                    "in handlers.py and in the retry path of client.py, and these are the only two callers",
                    "neither the import nor the export command writes to the log",
                ],
            },
            "false": {
                "meaning": "The scope is left open, with no list of where it holds",
                "examples": [
                    "the token is absent from every launched process",
                    "nothing anywhere logs the raw payload",
                ],
            },
        },
    },
    "about_tests": {
        "type": "noul",
        "instructions": "Is `criterion.text` a statement about tests -- that tests exist, or that tests cover, assert, show, prove, or pass something?",
        "criteria": {
            "true": "What must become true is a fact about tests",
            "false": "What must become true is a fact about the program, a file, or a command",
        },
    },
    "tests_preexist": {
        "type": "noul",
        "instructions": "Does `criterion.text` refer to tests that already exist, rather than to tests being introduced?",
        "criteria": {
            "true": "It names an existing suite, or tests written for something else, such as tests remaining green or another component's tests",
            "false": "It does not refer to any already-existing tests",
        },
    },
    "specificity": {
        "type": "score",
        "instructions": "How specifically does `criterion.text` name what must be true?",
        "criteria": [
            "Names no particular behavior, file, or place",
            "Names a behavior or a component in general terms",
            "Names a specific behavior at a specific place",
        ],
    },
}

#: `specificity` is collected but never labeled: it informs threshold choice
#: and has no ground truth to score against.


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


QUESTION_DIGEST = fingerprint(QUESTIONS)


# --- Corpus ----------------------------------------------------------------

def corpus() -> list[dict]:
    """Every acceptance criterion in the backlog, with where it came from."""
    items = []
    for path in sorted((ROOT / "tickets").glob("*.json")):
        document = json.loads(path.read_text())
        for task in document.get("tasks", []):
            for index, text in enumerate(task.get("acceptance_criteria") or [], 1):
                items.append({
                    "uid": f"{path.name}:{task['id']}:{index}",
                    "file": path.name, "task": task["id"], "index": index,
                    "text": text,
                })
    return items


def load_labels() -> dict[str, dict]:
    if not LABELS.is_file():
        return {}
    labels = {}
    for line in LABELS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        row = json.loads(line)
        labels[row["uid"]] = row
    return labels


def labeled_rows(labels: dict) -> list[dict]:
    """Rows a human actually finished. An unlabeled row is skipped, not guessed."""
    return [r for r in labels.values() if r.get("procedure") in PROCEDURE]


# --- extract ---------------------------------------------------------------

def extract(_) -> int:
    OUT.mkdir(exist_ok=True)
    existing = load_labels()
    items = corpus()
    if LABELS.is_file():
        print(f"{LABELS} exists with {len(labeled_rows(existing))} labeled of "
              f"{len(existing)} rows; merging new criteria only.")
    lines = [
        "// Study B labels. One JSON object per line; edit the null fields.",
        "//   procedure         : execution | reading | artifact | none",
        "//   unbounded_absence : true if it asserts an absence over a set it does not list",
        "//   joins_claims      : true if it states two independently-true requirements",
        "//   self_gradable     : true if the author's own new test could satisfy it alone",
        "// Leave a row's procedure null to exclude it from the report.",
        "//   labeled_by        : model-proposed | human. A model-proposed row is a",
        "//     first pass, NOT ground truth. Confirm or correct it and set human,",
        "//     or the study measures agreement with the thing under test.",
    ]
    for item in items:
        prior = existing.get(item["uid"], {})
        lines.append(json.dumps({
            **item,
            "procedure": prior.get("procedure"),
            "unbounded_absence": prior.get("unbounded_absence"),
            "joins_claims": prior.get("joins_claims"),
            "self_gradable": prior.get("self_gradable"),
            "labeled_by": prior.get("labeled_by"),
        }))
    LABELS.write_text("\n".join(lines) + "\n")
    measured = sum(1 for i in items if i["file"] == MEASURED_FILE)
    print(f"wrote {LABELS} with {len(items)} criteria "
          f"({measured} from {MEASURED_FILE}, the corpus CRITERIA.md measured).")
    print("Label them, then run: tools/study-b.py baseline")
    return 0


# --- baseline --------------------------------------------------------------

VOCABULARY = ["every", "all", "any", "no", "none", "never", "anywhere", "always",
              "each", "only", "cannot", "without", "unless", "nothing"]


def fires(words: tuple[str, ...], text: str) -> bool:
    low = text.lower()
    return any(re.search(r"\b" + w + r"\b", low) for w in words)


def scores(predicted: list[bool], actual: list[bool]) -> dict:
    tp = sum(1 for p, a in zip(predicted, actual) if p and a)
    fp = sum(1 for p, a in zip(predicted, actual) if p and not a)
    fn = sum(1 for p, a in zip(predicted, actual) if not p and a)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision,
            "recall": recall, "f1": f1}


def best_lexicon(rows: list[dict], field: str, *, max_words: int = 4) -> dict:
    """The keyword rule that scores best on these labels, over all small subsets.

    This is deliberately generous to the baseline: it is tuned on the same
    labels it is scored against, so it is an upper bound no honestly-written
    lexicon could exceed. The model has to beat that, not beat a guess.
    """
    actual = [bool(r.get(field)) for r in rows]
    best = {"words": (), "f1": -1.0}
    for size in range(1, max_words + 1):
        for combo in itertools.combinations(VOCABULARY, size):
            predicted = [fires(combo, r["text"]) for r in rows]
            result = scores(predicted, actual)
            if result["f1"] > best["f1"]:
                best = {"words": combo, **result}
    return best


def baseline(_) -> int:
    rows = labeled_rows(load_labels())
    if not rows:
        print("No labeled rows. Run extract, label, then retry.", file=sys.stderr)
        return 2
    print(f"{len(rows)} labeled criteria\n")
    measured = [r for r in rows if r["file"] == MEASURED_FILE]
    print(f"{'rule':<20} {'base rate':>10} {'best F1':>9}  best keyword rule")
    print("-" * 78)
    for field in ("unbounded_absence", "joins_claims", "self_gradable"):
        positives = sum(1 for r in rows if r.get(field))
        best = best_lexicon(rows, field)
        print(f"{field:<20} {positives:>4}/{len(rows):<5} {best['f1']:>9.2f}  "
              f"{'+'.join(best['words'])}  (P {best['precision']:.2f} R {best['recall']:.2f})")
    undecidable = sum(1 for r in rows if r["procedure"] == "none")
    print(f"\n{'procedure=none':<20} {undecidable:>4}/{len(rows):<5}   "
          f"(no keyword rule; rule 1 is not a vocabulary question)")
    if measured:
        print(f"\nOn {MEASURED_FILE} alone ({len(measured)} labeled of 128 total):")
        for field in ("unbounded_absence",):
            firing = [c for c in itertools.chain.from_iterable(
                itertools.combinations(VOCABULARY, n) for n in range(1, 5))
                if sum(fires(c, r["text"]) for r in measured) == 36]
            print(f"  lexicons firing on exactly 36: {len(firing)} "
                  f"-- the recorded count does not identify a rule")
    return 0


# --- ask -------------------------------------------------------------------

def load_cache() -> dict:
    return json.loads(CACHE.read_text()) if CACHE.is_file() else {}


def cache_key(text: str) -> str:
    return fingerprint({"text": text, "questions": QUESTION_DIGEST, "model": MODEL})


def call(text: str, timeout: float) -> dict:
    body = json.dumps({"state": {"criterion": {"text": text}},
                       "model": MODEL, "questions": QUESTIONS}).encode()
    request = urllib.request.Request(
        ENDPOINT, data=body, method="POST",
        headers={"Authorization": f"Bearer {os.environ[KEY_VARIABLE]}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def ask(arguments) -> int:
    OUT.mkdir(exist_ok=True)
    # Asking does not depend on labeling: the model sees only criterion text.
    # Prefer the labeled subset when one exists, so a partial labeling pass does
    # not pay for calls it will not score; otherwise ask about everything.
    labeled = labeled_rows(load_labels())
    rows = labeled or corpus()
    if not rows:
        print("No criteria found under tickets/.", file=sys.stderr)
        return 2
    if not labeled:
        print("No labels yet; asking about the whole corpus.")
    cache = load_cache()
    todo = [r for r in rows if cache_key(r["text"]) not in cache]
    print(f"{len(rows)} criteria, {len(rows) - len(todo)} cached, {len(todo)} to call.")
    if arguments.dry_run:
        characters = sum(len(r["text"]) for r in todo)
        print(f"would send ~{characters:,} characters of criterion text "
              f"in {len(todo)} requests to {ENDPOINT}")
        return 0
    if not todo:
        print("Nothing to do.")
        return 0
    if not os.environ.get(KEY_VARIABLE):
        print(f"{KEY_VARIABLE} is not set.", file=sys.stderr)
        return 2
    failures = 0
    for number, row in enumerate(todo, 1):
        for attempt in range(4):
            try:
                cache[cache_key(row["text"])] = {
                    "uid": row["uid"], "text": row["text"], "response": call(row["text"], arguments.timeout)}
                break
            except urllib.error.HTTPError as error:
                if error.code in (429, 529) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                print(f"  {row['uid']}: HTTP {error.code}", file=sys.stderr)
                failures += 1
                break
            except (urllib.error.URLError, OSError, ValueError) as error:
                print(f"  {row['uid']}: {error}", file=sys.stderr)
                failures += 1
                break
        if number % 10 == 0 or number == len(todo):
            CACHE.write_text(json.dumps(cache, indent=1))
            print(f"  {number}/{len(todo)}")
    CACHE.write_text(json.dumps(cache, indent=1))
    print(f"cached {len(cache)} responses in {CACHE}; {failures} failed.")
    return 1 if failures else 0


# --- report ----------------------------------------------------------------

THRESHOLDS = (0.50, 0.60, 0.70, 0.80, 0.90, 0.95)

#: Rule 6's second threshold, swept against these same labels rather than held
#: out. With nine positives and two thresholds tuned together this is the most
#: overfit number in the study; treat it as a starting point for a larger
#: corpus, not as a calibrated value.
PREEXIST_MAX = 0.65

#: Rule 3's second threshold. Same caveat as PREEXIST_MAX: swept in-sample.
PLACES_MAX = 0.65

#: Run-to-run variance, measured: rule 3 scored 0.36 and then 0.34 on two runs
#: of an unchanged question. A margin inside this band is not a result, so the
#: verdict below refuses to call one a win rather than leaving that to a reader
#: who may only look at the direction.
MARGIN = 0.05

#: (display name, hand-labeled field, model signal). Rule 3 appears twice so the
#: one-question and decomposed forms are scored on one run, which also controls
#: for the ~0.02 F1 of run-to-run variance between them.
RULES = (
    ("unbounded_absence (one question)", "unbounded_absence", "unbounded_absence"),
    ("unbounded_absence (decomposed)", "unbounded_absence", "unbounded_absence_v2"),
    ("joins_claims", "joins_claims", "joins_claims"),
    ("self_gradable", "self_gradable", "self_gradable"),
)


def model_signal(answers: dict, field: str, limit: float) -> bool:
    """Whether the model asserts one rule at this threshold.

    Rule 6 is composed from two questions rather than asked as one, so the
    combining happens here in code where it can be read and changed, rather
    than inside an instruction the model interprets.
    """
    if field == "unbounded_absence_v2":
        return (answers["asserts_absence"]["noul"] >= limit
                and answers["names_the_places"]["noul"] < PLACES_MAX)
    if field == "self_gradable":
        return (answers["about_tests"]["noul"] >= limit
                and answers["tests_preexist"]["noul"] < PREEXIST_MAX)
    return answers[field]["noul"] >= limit


def report(_) -> int:
    rows = labeled_rows(load_labels())
    cache = load_cache()
    have = [r for r in rows if cache_key(r["text"]) in cache]
    if not have:
        print("No labeled rows with cached responses. Run ask.", file=sys.stderr)
        return 2
    print(f"Study B -- {len(have)} labeled criteria with responses, model {MODEL}, "
          f"questions {QUESTION_DIGEST}")
    proposed = [r for r in have if r.get("labeled_by") == "model-proposed"]
    if proposed:
        print()
        print(f"   !! {len(proposed)} of {len(have)} labels are model-proposed and not")
        print("      human-confirmed. Every number below is agreement between a model")
        print("      and itself on those rows, which is not evidence about anything.")
        print("      Confirm them and set labeled_by to human before citing this.")
    print()

    verdicts = []
    for display, field, signal in RULES:
        actual = [bool(r.get(field)) for r in have]
        best = best_lexicon(have, field)
        print(f"## {display}   (positives {sum(actual)}/{len(have)})")
        print(f"   best keyword rule   F1 {best['f1']:.2f}  "
              f"P {best['precision']:.2f}  R {best['recall']:.2f}  "
              f"[{'+'.join(best['words'])}]")
        peak = 0.0
        for limit in THRESHOLDS:
            predicted = [
                model_signal(cache[cache_key(r["text"])]["response"]["answers"], signal, limit)
                for r in have]
            result = scores(predicted, actual)
            peak = max(peak, result["f1"])
            print(f"   noul >= {limit:.2f}      F1 {result['f1']:.2f}  "
                  f"P {result['precision']:.2f}  R {result['recall']:.2f}  "
                  f"(fires on {sum(predicted)})")
        verdicts.append((display, signal, peak, best["f1"]))
        print()

    actual = [r["procedure"] for r in have]
    print(f"## procedure   (4-way; 'none' is rule 1)")
    for limit in THRESHOLDS:
        answers = [cache[cache_key(r["text"])]["response"]["answers"]["procedure"]
                   for r in have]
        confident = [(a, t) for a, t in zip(answers, actual) if a["confidence"] >= limit]
        if not confident:
            print(f"   confidence >= {limit:.2f}   no answers at this threshold")
            continue
        correct = sum(1 for a, t in confident if a["choice"] == t)
        print(f"   confidence >= {limit:.2f}   accuracy {correct/len(confident):.2f} "
              f"on {len(confident)}/{len(have)} answered "
              f"({len(confident)/len(have):.0%} coverage)")
    print()

    print("## Verdict (JUDGMENT.md section 11.3)")
    # Only a rule that section 7.3 lets block is load-bearing. Rules 4 and 6
    # annotate whatever they score, so winning on one of them unlocks nothing:
    # counting all three equally would call a study a pass on a rule that never
    # gates anything.
    GATING = {"unbounded_absence", "unbounded_absence_v2"}
    for display, signal, model_f1, base_f1 in verdicts:
        mark = "beats" if model_f1 > base_f1 else "does NOT beat"
        role = "blocks" if signal in GATING else "annotates"
        print(f"   {display:<34} model {model_f1:.2f} {mark} tuned keyword {base_f1:.2f}   ({role})")
    gating = [(d, s_, m, b) for d, s_, m, b in verdicts if s_ in GATING]
    wins = [(d, s_, m, b) for d, s_, m, b in gating if m > b + MARGIN]
    narrow = [(d, s_, m, b) for d, s_, m, b in gating if b < m <= b + MARGIN]
    for d, _, m, b in narrow:
        print(f"\n   {d}: +{m - b:.2f} is inside the {MARGIN:.2f} noise band; not a win.")
    if gating and not wins:
        print()
        print("   The model loses on every rule that section 7.3 lets block, and rule 1")
        print("   (procedure == none) did not fire on this corpus at all. A win on an")
        print("   annotate-only rule does not unlock the blocking tier.")
        print("   What is withdrawn is the blocking tier and its slice, not the whole")
        print("   specification: the annotating rules stand on their own numbers.")
        print("   Record the result in docs/JUDGMENT.md section 11.4 before building.")
    elif wins:
        print(f"\n   {len(wins)} of {len(gating)} blocking formulations improve on a keyword rule")
        print("   tuned on the same labels. Proceed to slice 9 with these thresholds.")
        print()
        print("   Caveat, and it belongs in the write-up: both sides are selected on")
        print("   the labels they are scored against -- the keyword rule by subset")
        print("   search, the model by threshold sweep. These are in-sample numbers.")
        print(f"   With {len(have)} rows a held-out split is thin, so treat a narrow")
        print("   win as no win. Report the margin, not just the direction.")
    else:
        print("\n   No rule improves on a tuned keyword rule.")
        print("   Section 11.3 says the specification is withdrawn. Record this result")
        print("   in docs/JUDGMENT.md section 11 and stop.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Study B of docs/JUDGMENT.md: keyword rule versus calibrated model.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("extract", help="Write the labeling template from tickets/.")
    sub.add_parser("baseline", help="Best achievable keyword rule; no network.")
    caller = sub.add_parser("ask", help="Call the model; results are cached.")
    caller.add_argument("--dry-run", action="store_true")
    caller.add_argument("--timeout", type=float, default=15.0)
    sub.add_parser("report", help="Precision, recall, and the withdrawal verdict.")
    arguments = parser.parse_args(argv)
    return {"extract": extract, "baseline": baseline,
            "ask": ask, "report": report}[arguments.command](arguments)


if __name__ == "__main__":
    sys.exit(main())
