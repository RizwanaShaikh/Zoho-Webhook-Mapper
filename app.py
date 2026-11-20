"""
Zoho Forms Webhook Mapper v2 — maps BOTH sides of each webhook row, in your order.

Difference from v1 (webhook_mapper.py in Orochi247/Zoho-Webhook-Mapper):
  v1 renamed the LEFT Parameter Name of rows that Auto-Map had already created,
  so the row order (and therefore the payload / output PDF order) stayed in
  Zoho form order. v2 rewrites BOTH sides of each row, in the order of your
  input file: row i of the page becomes pair i of your file — left Parameter
  Name typed, right dropdown changed to the pair's Zoho label. An input file
  in official MLS PDF order therefore produces a webhook in MLS PDF order.

How rows are created:
  The script does not create rows and does not click anything except the
  row controls it fills. YOU create the rows: when the script pauses, click
  Zoho's "Auto-Map Fields" yourself so there is one row per form field,
  then press Enter. (The Auto-Map automation and the + / - button code were
  removed on 2026-11-20 — they overlapped this simpler flow.) Rows beyond
  the input's length are reported in extra_rows.txt and left in place;
  delete them in Zoho before saving.

Standard fields (the keys JSON files include them; the tool also adds any
that are missing, and dedupes by parameter name so each is mapped once):
  Top, before all file fields:    Property Type -> Property Type,
                                  Address -> Address
  Bottom, after all file fields:  task_id, FormID, UniqueID, Date_Created,
                                  env, added_time, ip_address, house_id and
                                  live_key (see STANDARD_TOP / STANDARD_BOTTOM).
  Pass --no-standard to turn the tool's own adding off (not recommended).

Hidden fields:
  Hidden fields (orange / yellow eye-slash icon in the form builder) are
  MAPPED like any other field when they are present in the input file. Most
  hidden fields are either standard fields (added above) or appear in the
  MLS PDF, in which case the input file carries them at their PDF position.

Setup:
    python -m pip install playwright

Run:
    python webhook_mapper_v2.py pairs.json --dry-run     # preview what will be mapped, no browser
    python webhook_mapper_v2.py pairs.json --inspect     # dump row/dropdown HTML to inspect.txt, no filling
    python webhook_mapper_v2.py pairs.json --limit 3     # test on 3 rows first
    python webhook_mapper_v2.py pairs.json               # full run
    python webhook_mapper_v2.py pairs.json --url <form integrate URL>

IMPORTANT: If the right dropdown is not found on a row, run --inspect once
and share inspect.txt — the selectors at the top of this file are the only
thing that should need adjusting. The script never clicks Save; you review
and save.
"""

import argparse
import json
import difflib
import re
import sys
from pathlib import Path

# Playwright is imported lazily in main() so that --dry-run and the
# input-loading logic work on machines without Playwright installed.

# --------------------------------------------------------------------------
# Config — edit these if your form / Zoho layout differs
# --------------------------------------------------------------------------

WEBHOOK_URL = (
    "https://forms.zoho.com/houzeo/form/"
    "testARNorthwestArkansasBoardofRealtorsNABORMLSData/integrate"
    # The NABOR test form (Shreyan's default, as in the GitHub repo's app.py).
    # Pass --url for any other form.
)

BROWSER_CHANNEL = "chrome"          # "chrome" or "msedge" (uses the browser already installed)
PROFILE_DIR = "./zoho_profile"      # keeps your login/MFA session between runs
FAILED_LOG = Path("failed_rows.txt")

TYPE_DELAY_MS = 5                   # per-keystroke delay; raise if Zoho misses key events
SETTLE_MS = 350                     # wait after each action so Zoho can update the row

# One webhook row (same container v1 used) and the left input inside it.
ROW = "div[elname='webhookPayloadParam']"
LEFT_INPUT = "div[elname='webhookPayloadParam'] input[elname='param_name']"

# The right-hand field control INSIDE one row. Confirmed from Zoho's actual
# DOM (DevTools screenshot, 2026-11-19):
#   row  = div.webhook_Payload[elname='webhookPayloadParam']
#   left = div[elname='paramNameDiv'] > input[elname='param_name']
#   right= div[elname='paramValueDiv'] > div[elname='selectParentDiv']
#          > select[elname='param_fld_select']  (a native <select> that
#          Select2 hides: class 'select2-hidden-accessible')
#   Each <option> has value/link_name = Zoho's internal field ID
#   (SingleLine, SingleLine2, Radio2, Dropdown26, ...) and its TEXT is the
#   field label shown to users (Address, County, List Price, ...).
# So the dropdown is matched by option TEXT, never by value.
FIELD_SELECT = "select[elname='param_fld_select']"

# Fallback selectors if a Zoho variant ever renders something else.
RIGHT_CONTROL_SELECTORS = [
    FIELD_SELECT,
    "select",
    "[elname='param_value']",
    "[elname='field_name']",
    "[elname='webhookField']",
    "input[readonly]",
    "input:not([elname='param_name'])",
    ".zf-select",
    "[class*='dropdown']",
]

EXTRA_LOG = Path("extra_rows.txt")

# Candidate selectors for a visible option inside an opened custom dropdown.
OPTION_SELECTORS = [
    "[role='listbox'] [role='option']",
    "[role='option']",
    ".dropdown-menu li",
    ".select2-results__option",
    "ul[class*='dropdown'] li",
    "div[class*='option']",
]

# Standard fields (Shreyan's layout rule, 2026-11-19). The keys JSON files
# include them; the tool also adds any that are missing here. If the input
# file already contains a pair with the same parameter name, the file's
# pair keeps its file position and the standard one is dropped.
# FormID and UniqueID both point at "Unique ID" on purpose — that is how
# the webhook screenshot shows them.
STANDARD_TOP = [
    ("Property Type", "Property Type"),
    ("Address", "Address"),
]
STANDARD_BOTTOM = [
    ("task_id", "task_id"),
    ("FormID", "Unique ID"),
    ("UniqueID", "Unique ID"),
    ("Date_Created", "Date-Time"),
    ("env", "env"),
    ("added_time", "Added Time"),
    ("ip_address", "IP Address"),
    ("house_id", "house_id"),
    ("live_key", "live_key"),
]

# --------------------------------------------------------------------------
# Input loading — returns a list of dicts:
#   {"parameter_name": str, "zoho_label": str | None, "candidates": [str]}
# --------------------------------------------------------------------------

def label_candidates(parameter_name, zoho_label):
    """Dropdown labels to try, best first.

    With an explicit zoho_label (master / pairs input) that is the only
    serious candidate. For a legacy flat list we fall back to suffixes of
    the parameter name: Address_Information_Parcel_Number ->
    'Information Parcel Number', 'Parcel Number', 'Number' (longest first).
    """
    candidates = []
    if zoho_label:
        candidates.append(zoho_label)
    words = parameter_name.split("_")
    for start in range(1, len(words)):
        cand = " ".join(words[start:])
        if cand and cand not in candidates:
            candidates.append(cand)
    return candidates


def _pair(parameter_name, zoho_label=None, match=None):
    return {
        "parameter_name": str(parameter_name),
        "zoho_label": zoho_label,
        "candidates": label_candidates(str(parameter_name), zoho_label),
        "match": match,
    }


def load_pairs(path):
    """Accepts, in order of preference:
    1. Master JSON: {"sections": [{"fields": [{"parameter_name", "zoho_label",
       "match"}, ...]}, ...]}
       Fields with match == "pdf_only" or no zoho_label are counted, not mapped.
    2. Pairs list:  [{"parameter_name": ..., "zoho_label": ...}, ...]
       (also accepts {"parameter"/"name"} and {"field"/"label"} keys)
    3. Object:      {"Parameter_Name": "Zoho Label", ...}  (values = labels)
    4. Legacy list: ["Parameter_Name", ...]  (label guessed from the name)
    Returns (pairs, stats_dict).
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    stats = {"pdf_only_skipped": 0, "total_in_file": 0}

    # 1. Master JSON
    if isinstance(data, dict) and isinstance(data.get("sections"), list):
        pairs = []
        for section in data["sections"]:
            for field in section.get("fields", []):
                stats["total_in_file"] += 1
                zoho = field.get("zoho_label")
                if field.get("match") == "pdf_only" or not zoho:
                    stats["pdf_only_skipped"] += 1
                    continue
                pairs.append(_pair(field["parameter_name"], zoho,
                                   match=field.get("match")))
        return pairs, stats

    # 2. Pairs list
    if isinstance(data, list) and data and isinstance(data[0], dict):
        pairs = []
        for item in data:
            stats["total_in_file"] += 1
            param = item.get("parameter_name") or item.get("parameter") or item.get("name")
            zoho = item.get("zoho_label") or item.get("field") or item.get("label")
            if not param:
                continue
            pairs.append(_pair(param, zoho, match=item.get("match")))
        return pairs, stats

    # 3. Object mapping
    if isinstance(data, dict):
        pairs = []
        for param, zoho in data.items():
            stats["total_in_file"] += 1
            pairs.append(_pair(param, zoho if isinstance(zoho, str) else None))
        return pairs, stats

    # 4. Legacy flat list
    if isinstance(data, list):
        pairs = []
        for item in data:
            stats["total_in_file"] += 1
            pairs.append(_pair(item, None))
        return pairs, stats

    raise ValueError("JSON must be a master object, a pairs list, an object, or a flat list")


def add_standard_fields(pairs):
    """Wrap the file's pairs with the standard top/bottom fields.

    Dedupe is by parameter_name: a pair already present in the file keeps
    its file position and the standard copy is dropped.
    """
    have = {p["parameter_name"] for p in pairs}
    top = [_pair(p, z) for p, z in STANDARD_TOP if p not in have]
    bottom = [_pair(p, z) for p, z in STANDARD_BOTTOM if p not in have]
    return top + pairs + bottom

# --------------------------------------------------------------------------
# Page actions
# --------------------------------------------------------------------------

def fill_left(row, parameter_name):
    box = row.locator("input[elname='param_name']").first
    box.scroll_into_view_if_needed()
    box.click()
    box.press("Control+A")
    box.press("Delete")
    box.press_sequentially(parameter_name, delay=TYPE_DELAY_MS)  # real key events
    box.press("Tab")
    if box.input_value() != parameter_name:
        raise RuntimeError(f"left value mismatch, got {box.input_value()!r}")


def _visible_option(page, candidate):
    """Find a visible dropdown option whose text is exactly `candidate`."""
    for selector in OPTION_SELECTORS:
        options = page.locator(selector)
        for i in range(options.count()):
            opt = options.nth(i)
            try:
                if opt.is_visible() and opt.inner_text().strip() == candidate:
                    return opt
            except Exception:
                continue
    # Last resort: exact text anywhere visible (Zoho renders options as plain items)
    try:
        opt = page.get_by_text(candidate, exact=True).last
        if opt.is_visible():
            return opt
    except Exception:
        pass
    return None


_SET_SELECT_BY_TEXT_JS = """
(el, label) => {
  const opts = Array.from(el.options).filter(o => o.text.trim() === label);
  if (opts.length === 0) return {found: false, count: 0};
  el.value = opts[0].value;
  // Notify Select2 (jQuery) and native listeners that the value changed,
  // so Zoho's visible Select2 box and its save/preview state stay in sync.
  if (window.jQuery) { window.jQuery(el).trigger('change'); }
  el.dispatchEvent(new Event('change', {bubbles: true}));
  el.dispatchEvent(new Event('input', {bubbles: true}));
  const sel = el.options[el.selectedIndex];
  return {found: true, count: opts.length, value: el.value,
          text: sel ? sel.text.trim() : ''};
}
"""


_GET_OPTION_TEXTS_JS = "el => Array.from(el.options).map(o => o.text.trim())"


def _norm_label(s):
    """Normalise a label for comparison: collapse whitespace runs and drop
    spaces around '/' — Zoho option text and file labels can differ only in
    spacing (e.g. 'Exchange/Broker' vs 'Exchange / Broker'). The value that
    gets set is always the dropdown's own original text, never this form."""
    s = re.sub(r"\s+", " ", s or "").strip()
    return re.sub(r"\s*/\s*", "/", s)


def _select_via_select2_ui(page, row, candidates):
    """Fallback: click the visible Select2 box in this row, type the label
    in its search box if one appears, and click the matching result."""
    box = row.locator(".select2-selection, .select2-container").first
    if box.count() == 0:
        return None
    try:
        box.scroll_into_view_if_needed()
        box.click()
        page.wait_for_timeout(SETTLE_MS)
    except Exception:
        return None
    for cand in candidates:
        # Select2 often shows a search input at the top of the open dropdown.
        try:
            search = page.locator(".select2-search__field").first
            if search.count() > 0 and search.is_visible():
                search.fill(cand)
                page.wait_for_timeout(SETTLE_MS)
        except Exception:
            pass
        opt = _visible_option(page, cand)
        if opt is not None:
            opt.click()
            page.wait_for_timeout(SETTLE_MS)
            return cand
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return None


def select_right(page, row, candidates):
    """Select the Zoho field in this row's right dropdown. Returns the
    candidate that worked, or raises RuntimeError listing what was tried.

    Primary path (confirmed DOM): the row contains a native
    select[elname='param_fld_select'] hidden by Select2. Playwright's
    select_option() can refuse hidden elements, so the value is set by
    exact option TEXT in JS and a change event is fired for Select2.
    If several options share the same text, the first is used and a
    warning is printed — check those rows manually.
    """
    select = row.locator(FIELD_SELECT).first
    if select.count() > 0:
        # Read the row's option list ONCE. Matching is done against this
        # list, so a label that is not there fails in about a second —
        # previously each candidate fell through to the Select2 UI scan,
        # which took minutes on a 170-row page before failing anyway.
        try:
            texts = select.evaluate(_GET_OPTION_TEXTS_JS)
        except Exception:
            texts = None
        if texts is not None:
            by_norm = {}
            for t in texts:
                by_norm.setdefault(_norm_label(t), t)
            for cand in candidates:
                target = by_norm.get(_norm_label(cand))
                if target is None:
                    continue
                try:
                    result = select.evaluate(_SET_SELECT_BY_TEXT_JS, target)
                except Exception:
                    result = None
                if result and result.get("found"):
                    if result.get("count", 1) > 1:
                        print(f"    [warn] label {target!r} matches {result['count']} dropdown options; used the first (value={result.get('value')})")
                    page.wait_for_timeout(SETTLE_MS)
                    return cand
                # Belt and braces: try Playwright's own select_option too.
                try:
                    select.select_option(label=target, force=True)
                    page.wait_for_timeout(SETTLE_MS)
                    return cand
                except Exception:
                    continue
            close = difflib.get_close_matches(
                _norm_label(candidates[0]),
                [_norm_label(t) for t in dict.fromkeys(texts)],
                n=3, cutoff=0.55)
            hint = f"; closest options in this row's dropdown: {close}" if close else ""
            raise RuntimeError(
                f"no <option> with exact text matched; tried {candidates}{hint} "
                "— the file's label probably differs from the Zoho label; fix the file")
        # Could not read the option list at all: last-resort UI path.
        used = _select_via_select2_ui(page, row, candidates)
        if used:
            return used
        raise RuntimeError(f"no <option> with exact text matched; tried {candidates}")
    # No native select in this row (unexpected Zoho variant): custom dropdown.
    used = _select_via_select2_ui(page, row, candidates)
    if used:
        return used
    raise RuntimeError(f"no {FIELD_SELECT} found in row and Select2 UI fallback failed; tried {candidates}")


def read_right_text(row):
    """Best-effort read of a row's current right-dropdown value, for logging."""
    for selector in RIGHT_CONTROL_SELECTORS:
        control = row.locator(selector).first
        if control.count() == 0:
            continue
        try:
            tag = control.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                return control.evaluate("el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : ''").strip()
            if tag == "input":
                return control.input_value().strip()
            return control.inner_text().strip()
        except Exception:
            continue
    return ""


def inspect_page(page):
    """Dump the HTML we need to fix selectors, without filling anything."""
    out = []
    rows = page.locator(ROW)
    out.append(f"ROW count: {rows.count()}")
    for i in range(min(rows.count(), 3)):
        try:
            out.append(f"\n--- ROW {i} outerHTML ---\n" + rows.nth(i).evaluate("el => el.outerHTML")[:6000])
        except Exception as e:
            out.append(f"ROW {i}: could not read HTML ({e})")
    try:
        parent_html = page.locator(ROW).first.locator("xpath=..").evaluate("el => el.outerHTML")
        out.append("\n--- ROW PARENT outerHTML (first 8000 chars) ---\n" + parent_html[:8000])
    except Exception as e:
        out.append(f"Parent HTML unavailable ({e})")
    # First row's dropdown: option count + a sample of option texts/values.
    try:
        info = page.locator(ROW).first.locator(FIELD_SELECT).evaluate(
            """el => ({count: el.options.length,
                       sample: Array.from(el.options).slice(0, 15).map(o => o.value + ' :: ' + o.text)})"""
        )
        out.append(f"\n--- FIRST ROW DROPDOWN ---\noptions: {info['count']}\n" + "\n".join(info["sample"]))
    except Exception as e:
        out.append(f"Dropdown info unavailable ({e})")
    Path("inspect.txt").write_text("\n".join(out), encoding="utf-8")
    print("Wrote inspect.txt — share it if the dropdown selectors need fixing.")

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Zoho webhook mapper v2 — sets both sides, in input order.")
    ap.add_argument("json_file")
    ap.add_argument("--url", default=WEBHOOK_URL)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-standard", action="store_true",
                    help="do NOT add the standard fields (Property Type + Address on top, "
                         "system fields + live_key at the bottom). Not recommended")
    ap.add_argument("--include-hidden", action="store_true",
                    help="(deprecated, ignored) hidden fields in the file are now always mapped")
    ap.add_argument("--include-common", action="store_true",
                    help="(deprecated, ignored) the standard fields are now always added; see --no-standard")
    ap.add_argument("--inspect", action="store_true",
                    help="open the page, dump row/dropdown HTML to inspect.txt, and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the pairs that would be mapped (including standard fields) and exit")
    args = ap.parse_args()

    pairs, stats = load_pairs(args.json_file)

    if args.include_hidden or args.include_common:
        print("Note: --include-hidden / --include-common are no longer needed — "
              "hidden fields in the file are mapped, and the standard fields are always added.")
    if not args.no_standard:
        pairs = add_standard_fields(pairs)

    if stats["pdf_only_skipped"]:
        print(f"Skipped {stats['pdf_only_skipped']} field(s) with no Zoho counterpart (pdf_only in the master).")

    pairs = pairs[args.start:]
    if args.limit:
        pairs = pairs[: args.limit]

    print(f"Ready to map {len(pairs)} row(s), starting at index {args.start}.")
    for i, p in enumerate(pairs[:10]):
        print(f"  {args.start + i}: {p['parameter_name']}  ->  {p['zoho_label'] or '(guess from name)'}")
    if len(pairs) > 10:
        print(f"  ... and {len(pairs) - 10} more")

    if args.dry_run:
        for i, p in enumerate(pairs):
            print(f"{args.start + i}\t{p['parameter_name']}\t{p['zoho_label'] or ''}\tcandidates={p['candidates']}")
        return

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright is not installed. Run: python -m pip install playwright")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE_DIR, channel=BROWSER_CHANNEL, headless=False
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url)

        input("Log in if needed, open the Webhooks configuration (Payload Mapping), "
              "click 'Auto-Map Fields' so there is one row per form field, then press Enter...")

        if args.inspect:
            inspect_page(page)
            ctx.close()
            return

        def process_row(index, row, pair):
            fill_left(row, pair["parameter_name"])
            used = select_right(page, row, pair["candidates"])
            print(f"[ok]   {index}: {pair['parameter_name']} -> {used}")

        def log_fail(index, pair, e):
            print(f"[FAIL] {index}: {pair['parameter_name']} -> {pair['zoho_label']} ({e})")
            with FAILED_LOG.open("a", encoding="utf-8") as f:
                f.write(f"{index}\t{pair['parameter_name']}\t{pair['zoho_label']}\t{e}\n")

        # --- Single flow: the user created the rows (Auto-Map Fields);
        # the script rewrites them in input order, both sides. ---
        total_rows = page.locator(ROW).count()
        print(f"Rows on page: {total_rows}, pairs to place: {len(pairs)}")
        if total_rows == 0:
            print("No rows found — check you are on the Payload Mapping page and that "
                  "you clicked 'Auto-Map Fields'. Nothing was filled.")
            ctx.close()
            return

        for offset, pair in enumerate(pairs):
            index = args.start + offset
            if index >= page.locator(ROW).count():
                log_fail(index, pair, RuntimeError(
                    "no row on the page for this pair — Auto-Map created fewer rows than the input has"))
                continue
            try:
                process_row(index, page.locator(ROW).nth(index), pair)
            except Exception as e:
                log_fail(index, pair, e)

        # --- Extra rows: fields Auto-Map created that the input does not use
        # (other property types, unused form fields). They sit after the last
        # mapped row. Report them and leave them in place — delete them in
        # Zoho before saving, or they will be sent with their Auto-Map names. ---
        total_rows = page.locator(ROW).count()
        used_rows = args.start + len(pairs)
        if total_rows > used_rows:
            extras = []
            for idx in range(used_rows, total_rows):
                extras.append((idx, read_right_text(page.locator(ROW).nth(idx))))
            with EXTRA_LOG.open("w", encoding="utf-8") as f:
                for idx, txt in extras:
                    f.write(f"{idx}\t{txt}\n")
            print(f"{len(extras)} extra row(s) beyond the input (indexes {used_rows}-{total_rows - 1}) — listed in {EXTRA_LOG}.")
            print("Leaving extra rows in place. Delete them in Zoho before saving; "
                  "they would otherwise be sent with their Auto-Map names.")

        input("Review the page, SAVE the webhook manually, then press Enter to close...")
        ctx.close()


if __name__ == "__main__":
    main()
