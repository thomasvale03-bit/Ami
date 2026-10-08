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
    run(sys.argv[1], sys.argv[2])
