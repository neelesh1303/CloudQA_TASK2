"""Command-line entry point: python -m nltest ..."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from .agent import TestAgent
from .browser import Browser

ICON = {"passed": "PASS", "failed": "FAIL", "blocked": "BLOCKED", "error": "ERROR"}
SECRET_HINT = re.compile(r"pass|secret|token|key|pwd", re.I)


def load_cases(args) -> list[dict]:
    cases = []
    if args.test:
        cases.append({"name": "cli-test", "test": args.test, "url": args.url})
    if args.file:
        path = Path(args.file)
        raw = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            for i, c in enumerate(json.loads(raw), 1):
                c = {"test": c} if isinstance(c, str) else c
                cases.append({"name": c.get("name") or f"case-{i}", "test": c["test"], "url": c.get("url") or args.url})
        else:
            # Plain text: one test case per block; blocks separated by blank lines.
            # An optional first line "url: https://..." sets that block's start URL; "# ..." lines are comments.
            for i, block in enumerate(re.split(r"\n\s*\n", raw), 1):
                lines = [l for l in block.strip().splitlines() if l.strip() and not l.lstrip().startswith("#")]
                if not lines:
                    continue
                url = args.url
                if lines[0].lower().startswith("url:"):
                    url = lines.pop(0).split(":", 1)[1].strip()
                if lines:
                    cases.append({"name": f"case-{len(cases) + 1}", "test": "\n".join(lines), "url": url})
    return cases


def parse_vars(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            sys.exit(f"--var expects NAME=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k.strip()] = os.path.expandvars(v)
    return out


def write_junit(path: Path, results):
    fails = sum(r.status == "failed" for r in results)
    errs = sum(r.status in ("error", "blocked") for r in results)
    rows = []
    for r in results:
        body = ""
        if r.status == "failed":
            body = f'<failure message="{escape(r.summary, {chr(34): "&quot;"})}"/>'
        elif r.status in ("error", "blocked"):
            body = f'<error type="{r.status}" message="{escape(r.summary, {chr(34): "&quot;"})}"/>'
        rows.append(f'  <testcase classname="nltest" name="{escape(r.name)}: {escape(r.test_case[:80], {chr(34): "&quot;"})}" '
                    f'time="{r.duration_s}">{body}<system-out>{escape(chr(10).join(r.steps_performed))}</system-out></testcase>')
    path.write_text(f'<?xml version="1.0" encoding="UTF-8"?>\n<testsuite name="nltest" tests="{len(results)}" '
                    f'failures="{fails}" errors="{errs}">\n' + "\n".join(rows) + "\n</testsuite>\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nltest", description="Run plain-English test cases in a real browser using Claude.")
    ap.add_argument("test", nargs="?", help='Test case in plain English, e.g. "Log in as a customer and verify the dashboard is shown"')
    ap.add_argument("-u", "--url", help="Start URL (optional when the test names a site)")
    ap.add_argument("-f", "--file", help="File of test cases (.txt blocks separated by blank lines, or .json list)")
    ap.add_argument("--var", action="append", metavar="NAME=value",
                    help="Test data the agent may use as {{NAME}}. Names containing pass/secret/token/key are never shown to the model.")
    ap.add_argument("--headed", action="store_true", help="Show the browser window")
    ap.add_argument("--model", default=os.environ.get("NLTEST_MODEL", "claude-opus-5-5"))
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--max-steps", type=int, default=50)
    ap.add_argument("--out", default="reports", help="Directory for reports and screenshots")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    cases = load_cases(args)
    if not cases:
        ap.error("give a test case as an argument or with --file")
    variables = parse_vars(args.var)
    secret_names = {k for k in variables if SECRET_HINT.search(k)}

    run_dir = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    agent = TestAgent(model=args.model, effort=args.effort, max_steps=args.max_steps, verbose=not args.quiet)

    results = []
    for i, c in enumerate(cases, 1):
        case_dir = run_dir / f"{i:02d}-{re.sub(r'[^a-z0-9]+', '-', c['test'].lower())[:40].strip('-')}"
        case_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n[{i}/{len(cases)}] {c['test']}" + (f"  @ {c['url']}" if c["url"] else ""), flush=True)
        browser = Browser(headless=not args.headed, out_dir=case_dir, secrets=variables).start()
        try:
            r = agent.run(browser, c["name"], c["test"], c["url"], variables, secret_names)
        except KeyboardInterrupt:
            browser.close()
            raise
        except Exception as e:  # keep the suite going
            from .agent import TestResult
            r = TestResult(name=c["name"], test_case=c["test"], status="error", summary=f"{type(e).__name__}: {e}")
        finally:
            browser.close()
        (case_dir / "result.json").write_text(json.dumps(asdict(r), indent=2, ensure_ascii=False), encoding="utf-8")
        results.append(r)
        print(f"  => {ICON.get(r.status, r.status.upper())}: {r.summary}  ({r.duration_s}s)", flush=True)

    (run_dir / "results.json").write_text(json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False), encoding="utf-8")
    write_junit(run_dir / "junit.xml", results)

    print("\n" + "=" * 70)
    for r in results:
        print(f"{ICON.get(r.status, r.status):8} {r.test_case.splitlines()[0][:60]}")
    counts = {s: sum(r.status == s for r in results) for s in ICON}
    print("-" * 70)
    print("  ".join(f"{k}: {v}" for k, v in counts.items()) + f"    report: {run_dir}")
    return 0 if all(r.status == "passed" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
