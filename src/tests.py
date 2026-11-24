"""Simple substring-based eval. Run: python src/tests.py [case-id ...]"""
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic

import env  # noqa: F401  (loads ../.env before bot creates its API client)
from bot import answer

CASES = json.loads((Path(__file__).parent / "cases.json").read_text())
# Setup problems, not test failures: abort instead of marking every case as FAIL.
INFRA_ERRORS = (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.APIConnectionError)


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


def evaluate(case: dict) -> tuple[str, list[str]]:
    try:
        reply = run_case(case)
    except INFRA_ERRORS:
        raise
    except Exception as e:
        return "", [f"error: {e!r}"]
    return reply, check(case, reply)


def main() -> int:
    wanted = set(sys.argv[1:])
    cases = [c for c in CASES if not wanted or c["id"] in wanted]
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(evaluate, cases))
    except INFRA_ERRORS as e:
        print(f"ABORTED: could not reach the API ({type(e).__name__}).")
        print("This is a setup problem (check ANTHROPIC_API_KEY / network), not a test failure.")
        return 2

    by_group = defaultdict(lambda: [0, 0])
    failed = 0
    for case, (reply, problems) in zip(cases, results):
        by_group[case["group"]][1] += 1
        by_group[case["group"]][0] += not problems
        failed += bool(problems)
        print(f"{'PASS' if not problems else 'FAIL'}  {case['id']}")
        if problems:
            print(f"      Q: {case['turns'][-1]}")
            print(f"      A: {reply.strip()[:300]}")
            for p in problems:
                print(f"      - {p}")

    print()
    for group, (ok, total) in by_group.items():
        print(f"{group:10} {ok}/{total}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
