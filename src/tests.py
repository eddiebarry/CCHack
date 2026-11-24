"""Substring-based eval with run history.
Usage: python src/tests.py [case-id ...] [--runs N] [--no-save]
"""
import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import anthropic

ROOT = Path(__file__).parent
ENV_FILE = ROOT.parent / ".env"
SHELL_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def load_env() -> None:
    """Load KEY=value lines from ../.env into os.environ (overrides the shell)."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip("\"'")


load_env()  # must run before bot creates its API client
from bot import MODEL, answer, client  # noqa: E402

RESULTS = ROOT.parent / "results"
CASES = json.loads((ROOT / "cases.json").read_text())
# Setup problems, not test failures: abort instead of marking every case as FAIL.
INFRA_ERRORS = (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.APIConnectionError)
RANK = {"FAIL": 0, "FLAKY": 1, "PASS": 2}


def digest(paths) -> str:
    h = hashlib.sha1()
    for p in sorted(paths):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:7]


def system_digest() -> str:
    """Fingerprint of everything that can change the bot's behavior (not the test cases)."""
    files = [ROOT / "bot.py", ROOT / "orders.json", *(ROOT / "prompts").glob("*"), *(ROOT / "docs").glob("*")]
    return digest(files)


def preflight() -> bool:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("ABORTED: ANTHROPIC_API_KEY is not set (shell or .env).")
        return False
    source = ".env" if ENV_FILE.exists() else "shell environment"
    print(f"API key {key[:11]}...{key[-4:]} ({len(key)} chars, from {source}), model {MODEL}")
    if not key.startswith("sk-ant-"):
        print("WARNING: the key should start with 'sk-ant-'. It was probably cut off at the start when copied.")
    try:
        client.messages.create(model=MODEL, max_tokens=5, messages=[{"role": "user", "content": "hi"}])
    except INFRA_ERRORS as e:
        print(f"ABORTED: API check failed ({type(e).__name__}). This is a setup problem, not a test failure.")
        if ENV_FILE.exists() and SHELL_KEY and SHELL_KEY != key:
            print(f".env overrides a different shell key ({SHELL_KEY[:11]}...{SHELL_KEY[-4:]}). "
                  "If the shell key is the good one, rename or delete .env.")
        else:
            print("Check the key above: is it the one you expect, and is it valid?")
        return False
    return True


def run_case(case: dict) -> str:
    history = []
    reply = ""
    for turn in case["turns"]:
        history.append({"role": "user", "content": turn})
        reply = answer(history)
        history.append({"role": "assistant", "content": reply})
    return reply


def check(case: dict, reply: str) -> list[str]:
    low = reply.lower()
    problems = []
    if case["any_of"] and not any(s.lower() in low for s in case["any_of"]):
        problems.append(f"missing any of {case['any_of']}")
    if case.get("also_any_of") and not any(s.lower() in low for s in case["also_any_of"]):
        problems.append(f"missing any of {case['also_any_of']}")
    for s in case["none_of"]:
        if s.lower() in low:
            problems.append(f"contains forbidden {s!r}")
    return problems


def evaluate(task: tuple[dict, int]) -> dict:
    case, _ = task
    try:
        reply = run_case(case)
    except INFRA_ERRORS:
        raise
    except Exception as e:
        return {"reply": "", "problems": [f"error: {e!r}"]}
    return {"reply": reply, "problems": check(case, reply)}


def status(passes: int, runs: int) -> str:
    return "PASS" if passes == runs else "FAIL" if passes == 0 else "FLAKY"


def load_previous() -> dict | None:
    path = RESULTS / "history.jsonl"
    if not path.exists() or not path.read_text().strip():
        return None
    return json.loads(path.read_text().strip().splitlines()[-1])


def report_changes(prev: dict, now: dict) -> None:
    print(f"\nSince last run ({prev['time']}):")
    changed_system = prev["system"] != now["system"]
    changed_cases = prev["cases"] != now["cases"]
    print(f"  bot/prompt/docs: {'CHANGED' if changed_system else 'unchanged'} ({prev['system']} -> {now['system']})")
    print(f"  cases.json:      {'CHANGED' if changed_cases else 'unchanged'} ({prev['cases']} -> {now['cases']})")
    better, worse = [], []
    for cid, (passes, runs) in now["cases_status"].items():
        if cid not in prev["cases_status"]:
            continue
        old, new = status(*prev["cases_status"][cid]), status(passes, runs)
        if RANK[new] > RANK[old]:
            better.append(f"{cid} ({old}->{new})")
        elif RANK[new] < RANK[old]:
            worse.append(f"{cid} ({old}->{new})")
    print(f"  better: {', '.join(better) or '-'}")
    print(f"  worse:  {', '.join(worse) or '-'}")
    if not (changed_system or changed_cases) and (better or worse):
        print("  note: nothing changed in the bot or cases, so these differences are run-to-run noise.")
    print(f"  score:  {prev['score']:.0%} -> {now['score']:.0%}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", help="case ids to run (default: all)")
    ap.add_argument("--runs", type=int, default=3, help="runs per case, to average out randomness")
    ap.add_argument("--no-save", action="store_true", help="don't record this run in results/")
    args = ap.parse_args()

    known = {c["id"] for c in CASES}
    if set(args.ids) - known:
        print(f"Unknown case ids: {sorted(set(args.ids) - known)}")
        return 2
    cases = [c for c in CASES if not args.ids or c["id"] in args.ids]
    full_run = len(cases) == len(CASES)

    if not preflight():
        return 2

    tasks = [(c, i) for c in cases for i in range(args.runs)]
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(evaluate, tasks))
    except INFRA_ERRORS as e:
        print(f"ABORTED: lost the API mid-run ({type(e).__name__}). Setup problem, not a test failure.")
        return 2

    per_case = defaultdict(list)
    for (case, _), outcome in zip(tasks, outcomes):
        per_case[case["id"]].append(outcome)

    by_group = defaultdict(lambda: [0, 0])
    cases_status = {}
    for case in cases:
        runs = per_case[case["id"]]
        passes = sum(not r["problems"] for r in runs)
        cases_status[case["id"]] = [passes, len(runs)]
        by_group[case["group"]][0] += passes
        by_group[case["group"]][1] += len(runs)
        st = status(passes, len(runs))
        print(f"{st:5} {passes}/{len(runs)}  {case['id']}")
        if st != "PASS":
            bad = next(r for r in runs if r["problems"])
            print(f"      Q: {case['turns'][-1]}")
            print(f"      A: {bad['reply'].strip()[:300]}")
            for p in bad["problems"]:
                print(f"      - {p}")

    total_pass = sum(p for p, _ in cases_status.values())
    total_runs = sum(n for _, n in cases_status.values())
    print()
    for group, (ok, total) in by_group.items():
        print(f"{group:10} {ok}/{total}")
    fully = sum(status(*v) == "PASS" for v in cases_status.values())
    flaky = sum(status(*v) == "FLAKY" for v in cases_status.values())
    print(f"\n{total_pass}/{total_runs} runs passed ({total_pass / total_runs:.0%}) | "
          f"{fully}/{len(cases)} cases always pass | {flaky} flaky")

    now = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "model": MODEL,
        "runs": args.runs,
        "system": system_digest(),
        "cases": digest([ROOT / "cases.json"]),
        "score": total_pass / total_runs,
        "cases_status": cases_status,
    }
    prev = load_previous()
    if prev:
        report_changes(prev, now)
    if full_run and not args.no_save:
        RESULTS.mkdir(exist_ok=True)
        with (RESULTS / "history.jsonl").open("a") as f:
            f.write(json.dumps(now) + "\n")
        (RESULTS / "last_run.json").write_text(json.dumps(per_case, indent=2))
        print("\nSaved to results/ (history.jsonl, last_run.json has every reply).")
    elif not full_run:
        print("\n(Partial run: not saved to history.)")
    return 0 if fully == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
