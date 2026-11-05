# nltest: plain-English tests, run in a real browser

`nltest` takes a test case written in plain English, such as *"Log in as a customer and verify the dashboard is shown"*. It carries the test out in Chromium (through Playwright) and reports **PASS / FAIL / BLOCKED** with evidence.

Claude (`claude-opus-5-5` by default) acts as the tester. Nothing is tied to one website: there are no selectors, page objects or per-site code.

## How it works

```
plain-English test ──► Claude ──tool calls──► Playwright browser
                        ▲                          │
                        └── page snapshot ◄────────┘
                 (URL, visible text, interactive elements as [e12] refs)
```

1. **Observe.** For each frame, an injected script lists the visible interactive elements (links, buttons, inputs, selects, ARIA roles). It gives each one a short ref such as `e12`, a role, an accessible name, and its value or state. Passwords are masked. The page's visible text is included as well.
2. **Act.** Claude picks tools such as `click(e12)`, `type_text(e5, "...")`, `select_option`, `press_key`, `hover`, `scroll`, `switch_tab`, `go_back` and `navigate`. After each action the page settles (load state plus network idle), and the next snapshot is sent back. New tabs, JS dialogs and same-origin or cross-origin iframes are handled.
3. **Verify.** Expected results are confirmed with a deterministic `check` tool (`text_visible`, `text_absent`, `url_contains`, `title_contains`, `element_visible`). Claude can also take a `screenshot` and look at it for visual checks. Every check is recorded in the report.
4. **Verdict.** Claude finishes with `report_result`:
   * **passed**: every step was done and every expectation was observed.
   * **failed**: the application didn't behave as the test expects.
   * **blocked**: the test couldn't be run for reasons outside the app (site down, CAPTCHA, missing data).
   * **error**: a tool or API problem, or the step limit was reached.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
set ANTHROPIC_API_KEY=sk-ant-...   # macOS/Linux: export ANTHROPIC_API_KEY=...
```

## Usage

```bash
# a single test (phptravels issues demo logins via the form at phptravels.com/demo; pass them in as variables)
python -m nltest "Log in as a customer and verify the dashboard is shown" --url https://phptravels.net \
    --var CUSTOMER_EMAIL=<demo email> --var CUSTOMER_PASSWORD=<demo password>

# a suite: text blocks separated by blank lines, optional "url:" line per block
python -m nltest -f examples/phptravels.txt
python -m nltest -f examples/other_sites.txt

# watch it run
python -m nltest "Search for hotels in Dubai and verify results are listed" -u https://phptravels.net --headed

# supply test data; names containing pass/secret/token/key are never shown to the model
python -m nltest "Log in with {{USER}} / {{PASSWORD}} and verify the account page opens" \
    -u https://example.com/login --var USER=alice --var PASSWORD=$MY_PW
```

| Option | Default | |
|---|---|---|
| `--url / -u` | none | Start URL. It can be omitted if the test names the site. |
| `--file / -f` | none | `.txt` (blocks) or `.json` (`[{"name","test","url"}]`) |
| `--var NAME=value` | none | Test data the agent types as `{{NAME}}`. The value is substituted locally. |
| `--model` | `claude-opus-5-5` | Any Claude model, also settable with `NLTEST_MODEL` |
| `--effort` | `medium` | `low` … `max`. Higher is more careful but slower. |
| `--max-steps` | 50 | Model turns before giving up |
| `--headed` | off | Show the browser window |
| `--out` | `reports/` | Where reports go |

The exit code is `0` only when every test passes, so it can gate CI.

## Output

Each run writes to `reports/<timestamp>/`:
* `results.json`: every case with status, summary, steps performed, checks (with pass/fail detail), the full action log, token usage and duration
* `junit.xml`: for CI dashboards
* `NN-<case>/`: per-case `result.json`, evidence screenshots and a final screenshot

## Design notes

* **Generality.** The only page knowledge comes from the live DOM. Credentials are found the way a human tester finds them: on the page, when a site prints them (the-internet, saucedemo), or from `--var`. phptravels now hands out demo logins only after a lead form (work email, WhatsApp number). The agent won't submit invented personal data to get them. Without credentials it reports **blocked** and names the variables to supply.
* **Faithful verdicts.** The prompt tells the model not to work around real defects or take shortcuts through URLs. A `passed` verdict should be backed by recorded `check` results, which the report keeps so a reviewer can audit them.
* **Robustness.** Refs are re-issued on every snapshot, so a stale ref returns a clear error and the model re-reads the page. Blocked clicks fall back to a DOM click. Failed fills fall back to keystrokes. Errors go back to the model as tool errors so it can recover.
* **Cost.** Prompt caching is on, and effort defaults to `medium`. Server-side refusal fallback (`fallbacks: "default"`) is enabled on models that support it.
