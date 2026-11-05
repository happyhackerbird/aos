"""Delivery check for an independent review task: the assigned exact revisions, a
verdict per requirement and the evidence files are present. A negative product
verdict passes this check; the coordinator judges the product separately.

With a native method the coordinator freezes the method's protocol requirements into
this command: ``--native-guard readonly`` demands that every child dispatch carried
``readonly: true`` as an argument (a prompt instruction does not count), and
``--native-models`` names the only models a child may run on, so a fallback the brief
did not predeclare fails delivery instead of being relabelled afterwards."""
import argparse
import json
import re
import sys
from pathlib import Path

FALLBACK_REASONS = ("credits", "auth", "slug rejected", "model absent")

parser = argparse.ArgumentParser()
parser.add_argument("--target", required=True)
parser.add_argument("--acceptance", required=True)
parser.add_argument("--feature", required=True)
parser.add_argument("--native-guard", action="append", default=[], choices=["readonly"],
                    help="argument every native child dispatch must carry (repeatable)")
parser.add_argument("--native-models", default="",
                    help="comma-separated models a native child may run on; anything else fails")
args = parser.parse_args()
declared_models = [m.strip() for m in args.native_models.split(",") if m.strip()]


def fail(msg):
    print(f"review delivery failed: {msg}", file=sys.stderr)
    sys.exit(1)


for value in (args.target, args.acceptance):
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        fail("assigned revisions must be full 40-hex SHAs")
try:
    report = json.loads(Path("reviews/review.json").read_text())
except (OSError, ValueError) as exc:
    fail(f"reviews/review.json: {exc}")
for field, expected in (("target_sha", args.target), ("acceptance_revision", args.acceptance), ("feature", args.feature)):
    if report.get(field) != expected:
        fail(f"{field} must equal the assigned value")
if report.get("verdict") not in ("accept", "accept with follow-ups", "reject"):
    fail("missing explicit product verdict")
requirements = report.get("requirements")
if not isinstance(requirements, dict) or not requirements:
    fail("requirements must map each numbered requirement to a verdict")
for name, row in requirements.items():
    if not re.fullmatch(r"[A-Z][0-9]+", name):
        fail(f"requirement key {name!r} must be a letter and a number, like R3 or S5")
    if not isinstance(row, dict) or row.get("status") not in ("met", "not met", "not exercised"):
        fail(f"{name}: invalid status")
    if not isinstance(row.get("evidence"), str) or not row["evidence"].strip():
        fail(f"{name}: missing evidence or limitation")
for field in ("reviewer", "method", "confirmed_findings", "suspected_findings", "unexpected_changes", "protected_inputs", "test_loading", "limitations"):
    if field not in report:
        fail(f"missing report field: {field}")
for filename in ("reviews/review.md", "reviews/target-check.txt"):
    if not Path(filename).is_file() or not Path(filename).read_text().strip():
        fail(f"missing delivery artifact: {filename}")
check = Path("reviews/target-check.txt").read_text()
if args.target not in check or args.acceptance not in check:
    fail("target-check.txt must record both exact revisions")

native_summary = ""
if report["method"] != "ordinary":
    for filename in ("reviews/native-method.md", "reviews/native-events.json"):
        if not Path(filename).is_file():
            fail(f"native method {report['method']!r} requires {filename}")
    try:
        events = json.loads(Path("reviews/native-events.json").read_text())
    except (OSError, ValueError) as exc:
        fail(f"native-events.json: {exc}")
    if not isinstance(events, dict) or not isinstance(events.get("records"), list):
        fail("native-events.json must hold a records list (empty only when the method was not exercised)")

    fallbacks = report.get("model_fallbacks")
    if not isinstance(fallbacks, list):
        fail("native method requires model_fallbacks: a list, empty when no child changed model")
    for i, row in enumerate(fallbacks):
        if not isinstance(row, dict):
            fail(f"model_fallbacks[{i}] must be an object")
        for key in ("from", "to", "reason", "evidence"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                fail(f"model_fallbacks[{i}]: missing {key}")
        if row["reason"] not in FALLBACK_REASONS:
            fail(f"model_fallbacks[{i}]: reason must be one of {', '.join(FALLBACK_REASONS)}; "
                 "a provider refusal over credits or auth is not a rejected slug")
        if declared_models and row["to"] not in declared_models:
            fail(f"model_fallbacks[{i}]: fallback to {row['to']!r} was not predeclared in --native-models")

    children = [r for r in events["records"]
                if isinstance(r, dict) and r.get("event") == "native_dispatch" and r.get("tool") == "Agent"]
    for child in children:
        arguments = child.get("arguments") if isinstance(child.get("arguments"), dict) else {}
        ref = child.get("tool_use_id") or child.get("uuid") or "<no id>"
        if "readonly" in args.native_guard and arguments.get("readonly") is not True:
            fail(f"child dispatch {ref} did not pass readonly: true; a read-only instruction in the prompt is not the guard")
        if declared_models:
            model = arguments.get("model")
            if model is None:
                fail(f"child dispatch {ref} names no model; every child must run on a predeclared model")
            if model not in declared_models:
                fail(f"child dispatch {ref} ran on {model!r}, which is not in the predeclared models {declared_models}")
    if (args.native_guard or declared_models) and not children:
        fail("the frozen command demands native guards but native-events.json holds no child dispatch; "
             "say in native-method.md that the method was not exercised and drop the guards, or capture the events")
    models_seen = sorted({(c.get("arguments") or {}).get("model") for c in children if isinstance(c.get("arguments"), dict)} - {None})
    native_summary = f"; native children: {len(children)}, models {models_seen or 'none'}, fallbacks {len(fallbacks)}"

statuses = [row["status"] for row in requirements.values()]
print(f"review delivered for {args.target}; verdict: {report['verdict']}; "
      f"{statuses.count('met')} met, {statuses.count('not met')} not met, {statuses.count('not exercised')} not exercised{native_summary}")
