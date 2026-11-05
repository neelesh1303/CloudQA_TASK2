"""Claude-driven test executor: turns a plain-English test case into browser actions."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import anthropic
from playwright.sync_api import Error as PWError

from .browser import Browser, BrowserError

# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

SYSTEM_PROMPT = """You are a meticulous QA automation engineer. You are given one test case written in plain English, and you carry it out in a real web browser using the tools provided, then report whether it passed or failed.

How to work:
- You see the page through snapshots: URL, title, visible text, and a list of interactive elements with refs like [e12]. Act on elements only by refs from the most recent snapshot; refs change whenever the page changes. Every action returns a fresh snapshot.
- Turn the test case into concrete steps and the expected results it implies. Carry out the steps the way a real user would: through the UI, in order. Don't type a URL to skip a step the test asks you to do through the UI, unless the test itself gives the URL.
- If the test needs data it doesn't give (credentials, a search term), look for it where a tester would: demo sites often show demo credentials on the page. Variables the operator supplied are listed in the task; type them as {{NAME}} placeholders and the real values are filled in for you. Never invent credentials when real ones are needed, and don't submit sign-up, contact or lead-capture forms with made-up personal details just to obtain credentials, unless the test case asks you to. If the credentials can't be found, report "blocked" and say which variables the operator should supply (e.g. CUSTOMER_EMAIL, CUSTOMER_PASSWORD).
- Pages can be slow, show cookie banners, open new tabs or display popups. Handle these sensibly (dismiss banners, switch tabs, wait briefly) and don't count them as failures.
- Before reporting "passed", verify each expected result with the `check` tool, and use `screenshot` when the expectation is visual. Base the verdict only on what you actually observed, never on what you assume.

Verdicts (call `report_result` exactly once, at the end):
- passed: every step was performed and every expected result was observed.
- failed: the application did not behave as the test expects. Examples: an error message, wrong content, missing element, a step that can't be completed because the app misbehaves, or a login rejected for credentials the test or site supplied.
- blocked: the test couldn't be carried out for reasons outside the application under test. Examples: the site is unreachable, a CAPTCHA or bot check you can't pass, required data unavailable anywhere, or an ambiguous test case.
Don't work around a real defect so that a test passes. Be efficient: no unnecessary steps."""

ELEMENT_REF = {"type": "string", "description": "Element ref from the latest snapshot, e.g. e12 or f1-e3"}


def _tool(name, description, props, required=None):
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": list(props) if required is None else required,
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool("navigate", "Open a URL in the current tab.", {"url": {"type": "string"}}),
    _tool("click", "Click an element.", {"ref": ELEMENT_REF}),
    _tool("type_text", "Clear an input/textarea and type text into it. Use {{NAME}} for operator-supplied variables. Set submit=true to press Enter afterwards.",
          {"ref": ELEMENT_REF, "text": {"type": "string"}, "submit": {"type": "boolean"}}),
    _tool("select_option", "Choose an option in a native <select> by its visible label (or value).",
          {"ref": ELEMENT_REF, "option": {"type": "string"}}),
    _tool("press_key", "Press a keyboard key or chord, e.g. Enter, Escape, Tab, ArrowDown.", {"key": {"type": "string"}}),
    _tool("hover", "Move the mouse over an element (e.g. to open a hover menu).", {"ref": ELEMENT_REF}),
    _tool("scroll", "Scroll the page.", {"direction": {"type": "string", "enum": ["up", "down"]}}),
    _tool("wait", "Wait for the page to finish something (1-10 seconds), then re-snapshot.",
          {"seconds": {"type": "integer", "minimum": 1, "maximum": 10}}),
    _tool("go_back", "Browser back button.", {}),
    _tool("switch_tab", "Make another open tab active (index from the Tabs line of the snapshot).",
          {"index": {"type": "integer", "minimum": 0}}),
    _tool("snapshot", "Re-read the current page without acting.", {}),
    _tool("screenshot", "Capture an image of the current viewport, for visual checks or evidence.", {}),
    _tool("check", "Deterministic assertion against the live page; the result is recorded as evidence in the report. "
          "Kinds: text_visible / text_absent (case-insensitive substring of the rendered text), url_contains, title_contains, element_visible (value = ref).",
          {"kind": {"type": "string", "enum": ["text_visible", "text_absent", "url_contains", "title_contains", "element_visible"]},
           "value": {"type": "string"}}),
    _tool("report_result", "Finish the test and give the verdict. Call this exactly once, as your final action.",
          {"status": {"type": "string", "enum": ["passed", "failed", "blocked"]},
           "summary": {"type": "string", "description": "One or two sentences: what happened and why this verdict."},
           "steps_performed": {"type": "array", "items": {"type": "string"}, "description": "The steps you actually carried out, in order."}}),
]


@dataclass
class TestResult:
    name: str
    test_case: str
    status: str = "error"  # passed | failed | blocked | error
    summary: str = ""
    steps_performed: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0})
    model: str = ""


class TestAgent:
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", max_steps: int = 50, verbose: bool = True):
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_steps = max_steps
        self.verbose = verbose

    def _log(self, msg: str):
        if self.verbose:
            print(msg, flush=True)

    def _call(self, messages):
        kwargs = dict(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
        )
        if self.model in FALLBACK_MODELS:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
            return self.client.beta.messages.create(**kwargs)
        return self.client.messages.create(**kwargs)

    def run(self, browser: Browser, name: str, test_case: str, start_url: str | None,
            variables: dict[str, str], secret_names: set[str]) -> TestResult:
        res = TestResult(name=name, test_case=test_case, model=self.model)
        t0 = time.time()

        if start_url:
            try:
                browser.navigate(start_url)
            except PWError as e:
                res.status, res.summary = "blocked", f"Could not open start URL {start_url}: {str(e).splitlines()[0]}"
                res.duration_s = round(time.time() - t0, 1)
                return res

        var_lines = []
        for k, v in variables.items():
            var_lines.append(f"- {{{{{k}}}}}" + ("" if k in secret_names else f" = {v!r}"))
        task = f"Test case:\n<test_case>\n{test_case}\n</test_case>\n\n"
        task += f"Start URL: {start_url}\n" if start_url else "No start URL given; if the test names a site, navigate to it.\n"
        if var_lines:
            task += "Operator-supplied variables (type them as placeholders):\n" + "\n".join(var_lines) + "\n"
        task += "\nCurrent page:\n" + (browser.snapshot() if start_url else "(blank tab)")
        messages = [{"role": "user", "content": task}]

        nudged = False
        for step in range(1, self.max_steps + 1):
            try:
                resp = self._call(messages)
            except anthropic.APIStatusError as e:
                res.status, res.summary = "error", f"Model API error {e.status_code}: {e.message}"
                break
            except anthropic.APIConnectionError as e:
                res.status, res.summary = "error", f"Could not reach the model API: {e}"
                break
            u = resp.usage
            res.usage["input_tokens"] += (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
            res.usage["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
            res.usage["output_tokens"] += u.output_tokens or 0

            if resp.stop_reason == "refusal":
                res.status, res.summary = "error", "The model declined to run this test case."
                break
            messages.append({"role": "assistant", "content": resp.content})
            for b in resp.content:
                if b.type == "text" and b.text.strip():
                    self._log(f"    · {b.text.strip()[:200]}")

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                if resp.stop_reason == "max_tokens":
                    messages.append({"role": "user", "content": "Continue."})
                    continue
                if nudged:
                    res.status, res.summary = "error", "The agent stopped without reporting a result."
                    break
                nudged = True
                messages.append({"role": "user", "content": "Continue the test. When you're done, call report_result."})
                continue

            results, finished = [], False
            for tu in tool_uses:
                if tu.name == "report_result":
                    res.status = tu.input.get("status", "error")
                    res.summary = tu.input.get("summary", "")
                    res.steps_performed = tu.input.get("steps_performed", [])
                    finished = True
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "Recorded."})
                    continue
                content, is_error = self._exec(browser, tu.name, tu.input, res)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content, "is_error": is_error})
            if finished:
                break
            if step == self.max_steps - 3:
                results.append({"type": "text", "text": "You are close to the step limit. Wrap up and call report_result soon."})
            messages.append({"role": "user", "content": results})
        else:
            res.status, res.summary = "error", f"Step limit ({self.max_steps}) reached without a verdict."

        try:
            _, path = browser.screenshot(f"final-{res.status}")
            if path:
                res.screenshots.append(str(path))
        except PWError:
            pass
        res.duration_s = round(time.time() - t0, 1)
        return res

    def _exec(self, b: Browser, name: str, args: dict, res: TestResult):
        shown = {k: v for k, v in args.items()}
        self._log(f"  → {name} {json.dumps(shown, ensure_ascii=False)[:160]}")
        rec = {"tool": name, "input": shown, "t": round(time.time(), 2)}
        try:
            if name == "snapshot":
                out = b.snapshot()
            elif name == "screenshot":
                data, path = b.screenshot("evidence")
                if path:
                    res.screenshots.append(str(path))
                rec["ok"] = True
                res.actions.append(rec)
                return [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
                        {"type": "text", "text": f"Screenshot of {b.page.url}" + b.drain_events()}], False
            elif name == "check":
                ok, detail = b.check(args["kind"], args["value"])
                res.checks.append({"kind": args["kind"], "value": args["value"], "passed": ok, "detail": detail})
                self._log(f"      {'✔' if ok else '✘'} {detail}")
                out = f"CHECK {'PASSED' if ok else 'FAILED'}: {detail}"
            else:
                if name == "navigate":
                    b.navigate(args["url"])
                elif name == "click":
                    b.click(args["ref"])
                elif name == "type_text":
                    b.type_text(args["ref"], args["text"], bool(args.get("submit")))
                elif name == "select_option":
                    b.select_option(args["ref"], args["option"])
                elif name == "press_key":
                    b.press_key(args["key"])
                elif name == "hover":
                    b.hover(args["ref"])
                elif name == "scroll":
                    b.scroll(args["direction"])
                elif name == "wait":
                    time.sleep(max(1, min(10, int(args["seconds"]))))
                elif name == "go_back":
                    b.go_back()
                elif name == "switch_tab":
                    b.switch_tab(int(args["index"]))
                else:
                    raise BrowserError(f"Unknown tool {name}")
                out = f"OK: {name} done." + b.drain_events() + "\n\n" + b.snapshot()
            rec["ok"] = True
            res.actions.append(rec)
            return out, False
        except (BrowserError, PWError, KeyError, ValueError) as e:
            msg = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
            rec["ok"], rec["error"] = False, msg
            res.actions.append(rec)
            self._log(f"      ! {msg}")
            try:
                snap = b.snapshot()
            except PWError:
                snap = "(snapshot unavailable)"
            return f"ERROR: {msg}" + b.drain_events() + "\n\nCurrent page:\n" + snap, True
