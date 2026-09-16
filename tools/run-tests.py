#!/usr/bin/env python3
"""Run the test suite as parallel per-module subprocesses.

`unittest discover` executes every module in one process, one after another.
This dispatches each module to its own subprocess instead, so a module's
environment and working-directory changes stay isolated, and several modules
run at once. The modules, their order within a module, and their assertions
are unchanged: only dispatch differs.

Modules are started longest-first from a recorded duration hint so the slow
ones do not land at the end of the schedule. An unknown module sorts first and
simply runs early.

Timing-sensitive modules spawn real subprocesses and assert on process-group
behavior under fixed timeouts. Oversubscribing the machine makes those flake,
so the default job count leaves headroom rather than using every core.

    tools/run-tests.py                 # all modules, default parallelism
    tools/run-tests.py --jobs 4
    tools/run-tests.py --serial        # identical dispatch to unittest discover
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
RAN = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)

# Measured on a 12-core host; used only to order the schedule.
SLOW_FIRST = ("test_recovery", "test_adaptive", "test_execution",
              "test_skill_execution", "test_parallel_execution", "test_workspaces")


def modules() -> list[str]:
    names = sorted(path.stem for path in TESTS.glob("test_*.py"))
    return sorted(names, key=lambda name: (SLOW_FIRST.index(name)
                                           if name in SLOW_FIRST else -1, name))


def run_module(name: str) -> tuple[str, int, int, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(TESTS), "-p", f"{name}.py"],
        cwd=ROOT, env=environment, capture_output=True, text=True,
    )
    output = completed.stdout + completed.stderr
    match = RAN.search(output)
    count = int(match.group(1)) if match else 0
    elapsed = time.monotonic() - started
    status = "ok" if completed.returncode == 0 else "FAIL"
    print(f"  {status:4} {name:34} {count:4} tests  {elapsed:6.1f}s", flush=True)
    return name, completed.returncode, count, output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=max(1, min(6, (os.cpu_count() or 2) // 2)))
    parser.add_argument("--serial", action="store_true", help="Run one module at a time.")
    arguments = parser.parse_args()
    jobs = 1 if arguments.serial else arguments.jobs
    if jobs < 1:
        parser.error("--jobs must be at least 1")

    names = modules()
    print(f"{len(names)} modules, {jobs} job(s)")
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(run_module, names))
    elapsed = time.monotonic() - started

    total = sum(count for _, _, count, _ in results)
    failed = [(name, output) for name, code, _, output in results if code != 0]
    print(f"\nRan {total} tests in {elapsed:.1f}s across {len(names)} modules")
    if not failed:
        print("OK")
        return 0
    for name, output in failed:
        print(f"\n{'=' * 70}\nFAILED: {name}\n{'=' * 70}\n{output}")
    print(f"FAILED ({len(failed)} module(s): {', '.join(name for name, _ in failed)})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
