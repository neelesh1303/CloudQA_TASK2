"""Site-agnostic browser layer built on Playwright.

The model never sees CSS selectors. Each snapshot tags the visible interactive
elements on the page (in every frame) with short refs like ``e12`` or ``f1-e3``.
The model picks a ref and this layer turns it back into a Playwright locator.
"""
from __future__ import annotations

import base64
import re
import time
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import Frame, Page, sync_playwright

SNAPSHOT_JS = r"""
(prefix) => {
  const MAX = 250;
  const SEL = [
    'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=tab]', '[role=menuitem]', '[role=checkbox]',
    '[role=radio]', '[role=option]', '[role=combobox]', '[role=switch]', '[role=textbox]',
    '[role=searchbox]', '[contenteditable=""]', '[contenteditable=true]', '[onclick]'
  ].join(',');
  document.querySelectorAll('[data-nlt]').forEach(e => e.removeAttribute('data-nlt'));
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.05;
  };
  const nameOf = el => {
    let n = el.getAttribute('aria-label') || '';
    const lb = el.getAttribute('aria-labelledby');
    if (!n && lb) n = lb.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ');
    if (!n && el.labels && el.labels.length) n = el.labels[0].innerText;
    const formy = ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName);
    if (!n && !formy) n = el.innerText || el.value || '';
    if (!n) n = el.title || el.alt || el.querySelector?.('img[alt]')?.alt || '';
    if (!n && formy) n = el.name || el.id || '';
    return clean(n).slice(0, 80);
  };
  const vh = window.innerHeight, vw = window.innerWidth;
  const lines = [];
  let count = 0, skipped = 0;
  for (const el of document.querySelectorAll(SEL)) {
    if (!visible(el)) continue;
    if (count >= MAX) { skipped++; continue; }
    const ref = prefix + 'e' + (++count);
    el.setAttribute('data-nlt', ref);
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role');
    let kind = role || tag;
    if (tag === 'input') kind = 'input[' + (el.type || 'text') + ']';
    const parts = [`[${ref}] ${kind}`];
    const nm = nameOf(el);
    if (nm) parts.push(`"${nm}"`);
    if (tag === 'input' || tag === 'textarea') {
      if (el.placeholder) parts.push(`placeholder="${clean(el.placeholder).slice(0, 50)}"`);
      if (['checkbox', 'radio'].includes(el.type)) parts.push(el.checked ? '(checked)' : '(unchecked)');
      else if (el.value) parts.push(el.type === 'password' ? 'value=<hidden>' : `value="${clean(el.value).slice(0, 50)}"`);
    }
    if (tag === 'select') {
      const opts = [...el.options].slice(0, 20).map(o => clean(o.text)).filter(Boolean);
      parts.push(`selected="${clean(el.selectedOptions[0]?.text)}" options=[${opts.join(' | ')}]`);
    }
    if (tag === 'a') {
      const h = el.getAttribute('href') || '';
      if (h && !h.startsWith('javascript')) parts.push(`href=${h.slice(0, 80)}`);
      if (el.target === '_blank') parts.push('(opens new tab)');
    }
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') parts.push('(disabled)');
    const r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) parts.push('(offscreen)');
    lines.push(parts.join(' '));
  }
  if (skipped) lines.push(`... ${skipped} more interactive elements not listed`);
  const text = (document.body?.innerText || '').split('\n').map(clean).filter(Boolean).join('\n');
  return { title: document.title, url: location.href, elements: lines, text };
}
"""


class BrowserError(Exception):
    pass


class Browser:
    def __init__(self, headless: bool = True, out_dir: Path | None = None,
                 secrets: dict[str, str] | None = None, action_timeout_ms: int = 8000):
        self.headless = headless
        self.out_dir = out_dir
        self.secrets = secrets or {}
        self.timeout = action_timeout_ms
        self.events: list[str] = []
        self._frames: dict[str, Frame] = {}
        self._shot_n = 0

    # ---------- lifecycle ----------
    def start(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self.context = self._browser.new_context(viewport={"width": 1366, "height": 900})
        self.context.set_default_timeout(self.timeout)
        self.context.on("page", self._on_new_page)
        self.page = self.context.new_page()
        self._wire(self.page)
        return self

    def close(self):
        for fn in (lambda: self.context.close(), lambda: self._browser.close(), lambda: self._pw.stop()):
            try:
                fn()
            except Exception:
                pass

    def _wire(self, page: Page):
        page.on("dialog", self._on_dialog)

    def _on_new_page(self, page: Page):
        if page is getattr(self, "page", None):
            return
        self._wire(page)
        self.page = page
        self.events.append("A new tab opened and is now the active tab.")

    def _on_dialog(self, dialog):
        self.events.append(f"Browser {dialog.type} dialog appeared with message {dialog.message!r}; it was accepted.")
        try:
            dialog.accept()
        except Exception:
            pass

    def drain_events(self) -> str:
        ev, self.events = self.events, []
        return ("\nEvents: " + " ".join(ev)) if ev else ""

    # ---------- helpers ----------
    def _settle(self):
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=self.timeout)
        except PWError:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=3000)
        except PWError:
            pass
        time.sleep(0.3)

    def _sub(self, text: str) -> str:
        def rep(m):
            key = m.group(1)
            if key not in self.secrets:
                raise BrowserError(f"Unknown variable {{{{{key}}}}}. Available: {', '.join(self.secrets) or 'none'}")
            return self.secrets[key]
        return re.sub(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", rep, text)

    def _locator(self, ref: str):
        ref = ref.strip().strip("[]")
        prefix = ref.rsplit("e", 1)[0] if "-" in ref else ""
        frame = self._frames.get(prefix)
        if frame is None or frame.is_detached():
            raise BrowserError(f"Ref {ref} is stale. Take a new snapshot and use a current ref.")
        loc = frame.locator(f'[data-nlt="{ref}"]')
        if loc.count() == 0:
            raise BrowserError(f"Ref {ref} no longer exists on the page (it probably changed). Use a ref from the latest snapshot.")
        return loc.first

    # ---------- observation ----------
    def snapshot(self, max_text: int = 3500) -> str:
        self._frames = {}
        sections = []
        tabs = self.context.pages
        if len(tabs) > 1:
            sections.append("Tabs: " + " | ".join(
                f"[{i}]{' (active)' if p is self.page else ''} {p.url[:80]}" for i, p in enumerate(tabs)))
        frames = [f for f in self.page.frames if not f.is_detached()][:6]
        for i, frame in enumerate(frames):
            prefix = "" if i == 0 else f"f{i}-"
            try:
                data = frame.evaluate(SNAPSHOT_JS, prefix)
            except PWError:
                continue
            self._frames[prefix] = frame
            if i == 0:
                sections.append(f"URL: {data['url']}\nTitle: {data['title']}")
                body = data["text"]
                if len(body) > max_text:
                    body = body[:max_text] + f"\n... [{len(data['text']) - max_text} more characters of page text]"
                sections.append("Visible page text:\n" + (body or "(none)"))
                sections.append("Interactive elements:\n" + ("\n".join(data["elements"]) or "(none)"))
            elif data["elements"]:
                sections.append(f"Elements inside iframe {prefix[:-1]} ({data['url'][:80]}):\n" + "\n".join(data["elements"]))
        return "\n\n".join(sections)

    def screenshot(self, label: str = "shot") -> tuple[str, Path | None]:
        png = self.page.screenshot(type="png")
        path = None
        if self.out_dir:
            self._shot_n += 1
            path = self.out_dir / f"{self._shot_n:02d}-{re.sub(r'[^a-z0-9]+', '-', label.lower())[:40]}.png"
            path.write_bytes(png)
        return base64.b64encode(png).decode(), path

    def check(self, kind: str, value: str) -> tuple[bool, str]:
        v = value.strip()
        if kind in ("text_visible", "text_absent"):
            needle = re.sub(r"\s+", " ", v).lower()
            found = False
            for f in self.page.frames:
                try:
                    txt = f.evaluate("() => document.body ? document.body.innerText : ''")
                except PWError:
                    continue
                if needle in re.sub(r"\s+", " ", txt).lower():
                    found = True
                    break
            ok = found if kind == "text_visible" else not found
            return ok, f"text {v!r} {'found' if found else 'not found'} on the page"
        if kind == "url_contains":
            url = self.page.url
            return v.lower() in url.lower(), f"current URL is {url}"
        if kind == "title_contains":
            t = self.page.title()
            return v.lower() in t.lower(), f"page title is {t!r}"
        if kind == "element_visible":
            try:
                vis = self._locator(v).is_visible()
            except BrowserError as e:
                return False, str(e)
            return vis, f"element {v} is {'visible' if vis else 'not visible'}"
        raise BrowserError(f"Unknown check kind {kind}")

    # ---------- actions ----------
    def navigate(self, url: str):
        if not re.match(r"^[a-z]+://", url):
            url = "https://" + url
        self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        self._settle()

    def click(self, ref: str):
        loc = self._locator(ref)
        try:
            loc.click(timeout=self.timeout)
        except PWError as e:
            # Overlays or animations can block a normal click; fall back to a DOM click.
            try:
                loc.evaluate("el => el.click()")
                self.events.append(f"Normal click was blocked ({str(e).splitlines()[0][:120]}); used a DOM click.")
            except PWError:
                raise
        self._settle()

    def type_text(self, ref: str, text: str, submit: bool):
        loc = self._locator(ref)
        value = self._sub(text)
        try:
            loc.fill(value, timeout=self.timeout)
        except PWError:
            loc.click(timeout=self.timeout)
            self.page.keyboard.press("Control+A")
            self.page.keyboard.type(value, delay=20)
        if submit:
            loc.press("Enter")
        self._settle()

    def select_option(self, ref: str, option: str):
        loc = self._locator(ref)
        try:
            loc.select_option(label=option, timeout=self.timeout)
        except PWError:
            loc.select_option(value=option, timeout=self.timeout)
        self._settle()

    def press_key(self, key: str):
        self.page.keyboard.press(key)
        self._settle()

    def hover(self, ref: str):
        self._locator(ref).hover(timeout=self.timeout)
        time.sleep(0.5)

    def scroll(self, direction: str):
        dy = 700 if direction == "down" else -700
        self.page.mouse.wheel(0, dy)
        time.sleep(0.6)

    def go_back(self):
        self.page.go_back(wait_until="domcontentloaded")
        self._settle()

    def switch_tab(self, index: int):
        pages = self.context.pages
        if not 0 <= index < len(pages):
            raise BrowserError(f"No tab {index}; there are {len(pages)} tabs.")
        self.page = pages[index]
        self.page.bring_to_front()
        self._settle()
