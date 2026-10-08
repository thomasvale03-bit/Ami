import os, re, json, sys
from urllib.parse import quote
from playwright.sync_api import sync_playwright

RLKEY="y5qzl4wxuest71sajox3vn4nu"
ROOT="https://www.dropbox.com/scl/fo/pswyw5h713qjsklqxgd5a/h"
FOLDER_TO_VENUE={
 "OMNIA NC":"OMNIA Nightclub","OMNIA DC":"OMNIA Dayclub","HAKKASAN":"Hakkasan Nightclub",
 "JEWEL NC":"JEWEL Nightclub","MARQUEE NC":"Marquee Nightclub","MARQUEE DC":"Marquee Dayclub",
 "TAO NC":"TAO Nightclub","TAO BEACH":"TAO Beach Dayclub","PALM TREE BEACH CLUB":"Palm Tree Beach Club",
 "LIQUID POOL LOUNGE":"Liquid Pool Lounge","LAVO PARTY BRUNCH":"LAVO Party Brunch"}
MONTHS={"01":"January","02":"February","03":"March","04":"April","05":"May","06":"June","07":"July",
 "08":"August","09":"September","10":"October","11":"November","12":"December"}

def url(*parts):
    p="/".join(quote(x) for x in parts)
    return f"{ROOT}/{p}?rlkey={RLKEY}&dl=0"

def names(page, u):
    page.goto(u, wait_until="domcontentloaded")
    try: page.wait_for_load_state("networkidle", timeout=20000)
    except Exception: pass
    page.wait_for_timeout(2500)
    return page.locator("div._sl-file-name-text_mzi4c_1").evaluate_all("els=>els.map(e=>e.textContent.trim())")

NOISE={"ss","glf","lv","omlv","mon","tue","wed","thu","fri","sat","sun","weekly","socials","nc","dc"}
def clean_artist(s):
    s=re.sub(r"_\d+x\d+.*$","",s)                 # drop size + ext
    s=re.sub(r"\.(jpg|jpeg|png)$","",s,flags=re.I)
    s=s.replace("_"," ").replace("-"," ").strip()
    toks=[t for t in s.split() if t]
    while toks and toks[0].lower() in NOISE:       # strip leading series/day codes
        toks.pop(0)
    out=" ".join(toks).title()
    out=re.sub(r"\bDj\b","DJ",out)
    return out

def parse(fn, venue):
    m=re.match(r"^(\d{2})(\d{2})(\d{2})_[A-Za-z0-9]+_(.+)$", fn)
    if not m: return None
    yy,mm,dd,rest=m.groups()
    if dd=="00": return None                       # weekly overview graphic, not a dated show
    try:
        date=f"20{yy}-{mm}-{dd}"; import datetime; datetime.date.fromisoformat(date)
    except Exception: return None
    art=clean_artist(rest)
    if not art or art.lower().startswith("weekly"): return None
    return date, art

def _week_bounds(week_name, year):
    """(start, end) dates for a folder like "October 07 - October 13", or None."""
    import datetime
    months_idx = {name: int(num) for num, name in MONTHS.items()}
    parts = re.findall(r"([A-Za-z]+)\s+(\d{1,2})", week_name)
    if len(parts) < 2:
        return None
    bounds = []
    for mon, day in parts[:2]:
        m = months_idx.get(mon.capitalize())
        if not m:
            return None
        try:
            bounds.append(datetime.date(year, m, int(day)))
        except ValueError:
            return None
    return min(bounds), max(bounds)


def _week_contains(week_name, target):
    """True if a Dropbox week folder spans target (a date)."""
    import datetime
    b = _week_bounds(week_name, target.year)
    if not b:
        return False
    start, end = b
    return start - datetime.timedelta(days=1) <= target <= end + datetime.timedelta(days=1)


def _week_overlaps(week_name, win_start, win_end):
    """True if a Dropbox week folder overlaps the window [win_start, win_end]."""
    b = _week_bounds(week_name, win_start.year)
    if not b:
        return False
    start, end = b
    return start <= win_end and end >= win_start


def _month_folders(win_start, win_end):
    """Dropbox month folder names (e.g. "10 October") the window touches."""
    import datetime
    seen, folders, d = set(), [], win_start
    while d <= win_end:
        key = (d.year, d.month)
        if key not in seen:
            seen.add(key)
            folders.append(f"{d.month:02d} {MONTHS[f'{d:%m}']}")
        d += datetime.timedelta(days=1)
    return folders


def _dismiss_cookie_banner(pg):
    """Dropbox's cookie-consent banner overlaps the flyer corner. Accept it once
    (sets a cookie for the whole context) so screenshots come out clean."""
    for sel in ("button:has-text('Accept All')", "button:has-text('Accept all')",
                "button:has-text('Allow all')", "[data-testid='cookie-consent'] button"):
        try:
            btn = pg.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn.click(timeout=2000)
                pg.wait_for_timeout(500)
                return True
        except Exception:
            continue
    return False


def download_week_images(out_dir, target=None, start=None, end=None):
    """Save a week's flyer JPGs to out_dir (one per venue/show) by screenshotting
    each image preview, the method Dropbox doesn't block. Returns the saved file
    paths. Best-effort: skips anything it can't render.

    The window is [start, end] inclusive; if not given it's the Mon–Sun week of
    target (default today). A Mon–Sun week can straddle two of TAO's Dropbox week
    folders (and month folders), so this walks every folder that overlaps the
    window and keeps only flyers whose own date falls inside it. Keeps only the
    feed-ratio (1080x1350) image per show, so each event appears once."""
    import datetime
    target = target or datetime.date.today()
    if start is None or end is None:
        start = target - datetime.timedelta(days=target.weekday())  # Monday
        end = start + datetime.timedelta(days=6)                     # Sunday
    os.makedirs(out_dir, exist_ok=True)
    saved = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, executable_path=os.environ["CHROMIUM_PATH"])
        pg = b.new_context(locale="en-US").new_page(); pg.set_default_timeout(20000)
        _dismiss_cookie_banner(pg)  # once per context, before any screenshot
        for month_folder in _month_folders(start, end):
            try:
                week_names = names(pg, url(month_folder))
            except Exception as exc:  # noqa: BLE001 - a missing month folder is fine
                print("skip month", month_folder, exc, file=sys.stderr)
                continue
            weeks = [w for w in week_names if _week_overlaps(w, start, end)]
            print("month", month_folder, "weeks:", weeks, file=sys.stderr)
            for wk in weeks:
                for vf in names(pg, url(month_folder, wk)):
                    venue = FOLDER_TO_VENUE.get(vf.strip().upper())
                    if not venue:
                        continue
                    for fn in names(pg, url(month_folder, wk, vf)):
                        if not fn.lower().endswith((".jpg", ".jpeg", ".png")):
                            continue
                        if "1080x1920" in fn:   # skip story format; keep feed (1080x1350)
                            continue
                        parsed = parse(fn, venue)  # (date, artist); None = undated graphic
                        if not parsed:
                            continue
                        fdate = datetime.date.fromisoformat(parsed[0])
                        if not (start <= fdate <= end):  # outside this week's window
                            continue
                        try:
                            pg.goto(url(month_folder, wk, vf, fn), wait_until="domcontentloaded")
                            pg.wait_for_timeout(2500)
                            _dismiss_cookie_banner(pg)  # reappears until the cookie sticks
                            img = pg.locator("img[src*='previews'], img.sl-preview-image, "
                                             "div[data-testid='preview-content'] img").first
                            img.wait_for(timeout=15000)
                            dest = os.path.join(out_dir, re.sub(r"[^A-Za-z0-9._-]", "_", fn))
                            img.screenshot(path=dest)
                            saved.append(dest)
                        except Exception as exc:  # noqa: BLE001 - one bad flyer must not sink the batch
                            print("skip", fn, exc, file=sys.stderr)
        b.close()
    print("images saved:", len(saved), file=sys.stderr)
    return saved


def run(month_folder, out):
    data={}
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True, executable_path=os.environ["CHROMIUM_PATH"])
        pg=b.new_context(locale="en-US").new_page(); pg.set_default_timeout(20000)
        weeks=[w for w in names(pg, url(month_folder)) if re.search(r"\d", w) and "archive" not in w.lower()]
        print("weeks:", weeks, file=sys.stderr)
        for wk in weeks:
            vfolders=names(pg, url(month_folder, wk))
            for vf in vfolders:
                venue=FOLDER_TO_VENUE.get(vf.strip().upper())
                if not venue: continue
                for fn in names(pg, url(month_folder, wk, vf)):
                    r=parse(fn, venue)
                    if r:
                        date,art=r
                        data.setdefault(date, {}).setdefault(venue, art)
        b.close()
    json.dump(data, open(out,"w"), indent=2, sort_keys=True)
    print("dates:", len(data), file=sys.stderr)
    return data

if __name__=="__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "images":
        download_week_images(sys.argv[2])
    else:
        run(sys.argv[1], sys.argv[2])
