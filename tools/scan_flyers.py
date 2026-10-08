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

def _week_contains(week_name, target):
    """True if a Dropbox week folder like "October 07 - October 13" spans target
    (a date). Year isn't in the name, so we match on month+day within +-4 days
    either side of its range to stay robust around month boundaries."""
    months_idx = {name: int(num) for num, name in MONTHS.items()}
    parts = re.findall(r"([A-Za-z]+)\s+(\d{1,2})", week_name)
    if len(parts) < 2:
        return False
    import datetime
    bounds = []
    for mon, day in parts[:2]:
        m = months_idx.get(mon.capitalize())
        if not m:
            return False
        try:
            bounds.append(datetime.date(target.year, m, int(day)))
        except ValueError:
            return False
    start, end = min(bounds), max(bounds)
    return start - datetime.timedelta(days=1) <= target <= end + datetime.timedelta(days=1)


def download_week_images(out_dir, target=None):
    """Save the current week's flyer JPGs to out_dir (one per venue/show) by
    screenshotting each image preview, the method Dropbox doesn't block. Returns
    the list of saved file paths. Best-effort: skips anything it can't render."""
    import datetime
    target = target or datetime.date.today()
    os.makedirs(out_dir, exist_ok=True)
    month_folder = f"{int(f'{target:%m}'):02d} {MONTHS[f'{target:%m}']}"
    saved = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, executable_path=os.environ["CHROMIUM_PATH"])
        pg = b.new_context(locale="en-US").new_page(); pg.set_default_timeout(20000)
        weeks = [w for w in names(pg, url(month_folder)) if _week_contains(w, target)]
        print("week match:", weeks, file=sys.stderr)
        for wk in weeks:
            for vf in names(pg, url(month_folder, wk)):
                venue = FOLDER_TO_VENUE.get(vf.strip().upper())
                if not venue:
                    continue
                for fn in names(pg, url(month_folder, wk, vf)):
                    if not fn.lower().endswith((".jpg", ".jpeg", ".png")):
                        continue
                    if not parse(fn, venue):  # skip weekly-overview / undated graphics
                        continue
                    try:
                        pg.goto(url(month_folder, wk, vf, fn), wait_until="domcontentloaded")
                        pg.wait_for_timeout(2500)
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
