"""Tiny stand-in for urvenue.me / guestlist.urvenue.me used by the browser
tests (layout modelled on the Nov 21 2026 screenshots: month grid with date
labels at each cell's top-right, venue switcher top-right, event page with
'<Event> - Open', '+ Add Guest' modal with Select Item / owner / textarea).
Never talks to the real site."""
import json
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PASSWORD = "pw-test-only"

STYLE = """
body{margin:0;font-family:Arial;font-size:12px;width:1280px}
#side{position:absolute;left:0;top:0;width:138px;height:720px;background:#2a9d8f;color:#fff}
#top{position:absolute;left:138px;top:0;width:1142px;height:32px;background:#fff}
#switch{position:absolute;right:30px;top:8px;cursor:pointer}
#menu{position:absolute;right:30px;top:34px;background:#fff;border:1px solid #ccc;display:none}
#menu div{padding:6px 12px;cursor:pointer}
#main{position:absolute;left:155px;top:48px;width:850px}
#hdr{font-size:16px} #arrows{position:absolute;right:0;top:0}
#arrows button{width:26px;height:20px}
#grid{position:relative;margin-top:30px;width:826px;height:425px}
.cell{position:absolute;width:117px;height:84px;background:#e8e8e8;border:1px solid #ddd;box-sizing:border-box}
.lab{position:absolute;right:2px;top:2px;background:#6cb4ee;color:#fff;padding:1px 4px;font-size:9px}
.ev{position:absolute;left:2px;right:2px;top:18px;bottom:2px;background:#444;color:#fff;cursor:pointer;
    text-align:center;padding-top:40px;box-sizing:border-box}
.modal{position:absolute;left:375px;top:15px;width:265px;background:#fff;border:1px solid #999;padding:10px}
table{margin-top:10px}
"""


def calendar_html(venue, acct, vid, events, start):
    return f"""<html><head><style>{STYLE}</style></head><body>
<div id=side><div>URGUESTLIST</div><div>LIV LAS VEGAS</div><div>Thomas Vale</div></div>
<div id=top><span>Day View</span><span id=switch>{venue}</span>
<div id=menu></div></div>
<div id=main><span id=hdr></span><span id=arrows><button id=prev>&lsaquo;</button><button id=next>&rsaquo;</button></span>
<div id=grid></div></div>
<script>
const EVENTS = {json.dumps(events)};
const VENUES = window.__VENUES || [];
let start = new Date('{start}T12:00:00');
const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];
function iso(d){{return d.toISOString().slice(0,10);}}
function render(){{
  const g = document.getElementById('grid'); g.innerHTML='';
  const mid = new Date(start); mid.setDate(mid.getDate()+14);
  const hm = (typeof month === 'undefined') ? start : month;
  document.getElementById('hdr').innerText = MONTHS[hm.getMonth()] + ' ' + hm.getFullYear();
  for (let i=0;i<35;i++) {{
    const d = new Date(start); d.setDate(d.getDate()+i);
    const c = document.createElement('div'); c.className='cell';
    c.style.left = (i%7)*118 + 'px'; c.style.top = Math.floor(i/7)*85 + 'px';
    const l = document.createElement('span'); l.className='lab'; l.innerText = MON[d.getMonth()] + ' ' + d.getDate();
    c.appendChild(l);
    const ev = EVENTS[iso(d)];
    if (ev) {{
      const e = document.createElement('div'); e.className='ev'; e.innerText = ev.name;
      const y = String(d.getFullYear()).slice(2), m = String(d.getMonth()+1).padStart(2,'0'), dd = String(d.getDate()).padStart(2,'0');
      e.onclick = () => {{ location.href = '/2/{acct}/{vid}/guestlist/dayview/' + y + m + dd + '/0'; }};
      c.appendChild(e);
    }}
    g.appendChild(c);
  }}
}}
// Like urvenue: the first view starts this week; arrows jump to the week
// holding the 1st of the next / previous month (header = that month).
let month = new Date(start.getFullYear(), start.getMonth(), 1, 12);
function goMonth(delta){{
  month = new Date(month.getFullYear(), month.getMonth()+delta, 1, 12);
  start = new Date(month); start.setDate(start.getDate() - ((start.getDay()+6)%7)); render(); }}
document.getElementById('next').onclick = () => goMonth(1);
document.getElementById('prev').onclick = () => goMonth(-1);
document.getElementById('switch').onclick = () => {{
  const m = document.getElementById('menu'); m.style.display='block'; m.innerHTML='';
  for (const [name, href] of {json.dumps(VENUE_MENU)}) {{
    const o = document.createElement('div'); o.innerText = name; o.onclick = () => {{ location.href = href; }}; m.appendChild(o);
  }}
}};
render();
</script></body></html>"""


VENUE_MENU = [["LIV Beach", "/2/2386/1214881/guestlist/dayview"], ["LIV Las Vegas", "/2/2386/777/guestlist/dayview"]]


def event_html(venue, ev, night):
    return f"""<html><head><style>{STYLE}</style></head><body>
<div id=side><div>URGUESTLIST</div><div>Thomas Vale</div></div>
<div id=top><span>Guest Lists <u>{night.strftime('%b')} {night.day}, {night.year}</u></span><span id=switch>{venue}</span></div>
<div id=main>
<div><b>{ev['name']}</b> - {ev['status']}</div>
<div><b>Operation Hours</b></div><div>From {ev['hours']}</div>
<button id=cal>Calendar</button> <button id=add>+ Add Guest</button>
<input id=q placeholder="Type to search...">
<table id=t><thead><tr><th>STATUS</th><th>NAME</th><th>ITEM</th><th>OWNER</th><th>GUESTS</th><th>MALES</th><th>FEMALES</th></tr></thead><tbody></tbody></table>
</div>
<script>
const KEY = 'rows-' + location.pathname;
function rows(){{ return JSON.parse(localStorage.getItem(KEY) || {json.dumps(json.dumps(ev.get('rows', [])))}); }}
function draw(){{
  const q = document.getElementById('q').value.toLowerCase();
  const tb = document.querySelector('#t tbody'); tb.innerHTML='';
  const rs = rows().filter(r => !q || r.name.toLowerCase().includes(q));
  if (!rs.length) {{ tb.innerHTML = '<tr><td colspan=7>No data to display</td></tr>'; return; }}
  for (const r of rs) tb.innerHTML += `<tr><td>Active</td><td>${{r.name}}</td><td>General List</td><td>Thomas Vale</td><td>${{r.m+r.f}}</td><td>${{r.m}}</td><td>${{r.f}}</td></tr>`;
}}
document.getElementById('q').oninput = draw;
// The real site uses an Angular ng-select: its search <input> sits on top of
// the "- Select Item -" placeholder and swallows clicks (Nov 21 2026 crash).
const NG = {json.dumps(bool(ev.get('ng')))};
const NGSEL = `<ng-select class="ng-select" id=item style="display:block;width:300px">
  <div class="ng-select-container" style="position:relative;height:34px;border:1px solid #ccc">
    <div class="ng-value-container"><div class="ng-placeholder" style="padding:6px">- Select Item -</div>
      <div class="ng-input" style="position:absolute;left:0;top:0;right:0;bottom:0">
        <input type=text style="width:100%;height:100%;opacity:0.01;border:0"></div></div>
    <span class="ng-arrow-wrapper"></span></div></ng-select>`;
function ngWire(d){{
  const ng = d.querySelector('ng-select'); if (!ng) return;
  const open = () => {{
    if (ng.querySelector('ng-dropdown-panel')) return;
    const p = document.createElement('ng-dropdown-panel'); p.className='ng-dropdown-panel';
    p.style.display='block';
    p.innerHTML = '<div class="ng-dropdown-panel-items"><div class="ng-option"><span class="ng-option-label">VIP List</span></div><div class="ng-option"><span class="ng-option-label">General List</span></div></div>';
    ng.appendChild(p);
    p.querySelectorAll('.ng-option').forEach(o => o.addEventListener('mousedown', e => {{
      e.preventDefault(); ng.dataset.value = o.textContent.trim();
      ng.classList.add('ng-has-value');
      ng.querySelector('.ng-placeholder').style.display='none';
      let v = ng.querySelector('.ng-value'); if (!v) {{ v = document.createElement('div'); v.className='ng-value';
        ng.querySelector('.ng-value-container').prepend(v); }}
      v.innerHTML = '<span class="ng-value-label">' + o.textContent.trim() + '</span>';
      p.remove(); }}));
  }};
  ng.querySelector('.ng-select-container').addEventListener('mousedown', open);
}}
function itemValue(d){{ const ng = d.querySelector('ng-select'); return ng ? (ng.dataset.value||'') : d.querySelector('#item').value; }}
const NO_ITEMS = {json.dumps(bool(ev.get('no_items')))};
document.getElementById('add').onclick = () => {{
  if (NO_ITEMS) {{  // real site: SweetAlert, no form (LIV NYE, Nov 21 2026)
    const w = document.createElement('div'); w.className='swal2-popup swal2-modal swal2-show';
    w.innerHTML = '<h2>Oops..</h2><div>There are no items available</div><button class="swal2-confirm">OK</button>';
    document.body.appendChild(w); w.querySelector('button').onclick = () => w.remove(); return;
  }}
  const d = document.createElement('div'); d.className='modal show'; d.id='modal';
  d.innerHTML = `<div>Add Guests <button class=close aria-label=Close>&times;</button></div>
    ${{NG ? NGSEL : '<select id=item><option>- Select Item -</option><option>General List</option></select>'}}
    <select id=owner><option selected>Thomas Vale</option><option>Someone Else</option></select>
    <div>Add your booking below</div><textarea id=ta rows=8></textarea>
    <button id=go>Add Guests</button>`;
  document.body.appendChild(d); ngWire(d);
  d.querySelector('.close').onclick = () => d.remove();
  d.querySelector('#go').onclick = () => {{
    if (itemValue(d) !== 'General List') {{ alert('Select Item'); return; }}
    const line = d.querySelector('#ta').value.trim();
    const m = line.match(/^(.*?)\\s+(\\S+@\\S+)\\s+(.*?)\\s*(!.*)?$/);
    const mm = (line.match(/(\\d+)M\\b/)||[0,0])[1]*1, ff = (line.match(/(\\d+)F\\b/)||[0,0])[1]*1;
    const all = rows(); all.push({{name: m[1], email: m[2], m: mm, f: ff, comment: m[4]||''}});
    localStorage.setItem(KEY, JSON.stringify(all));
    window.__lastLine = line;
    d.remove(); draw();
  }};
}};
draw();
</script></body></html>"""


LOGIN_HTML = f"""<html><body><h3>urvenue</h3>
<div id=s1><input type=email id=email><button id=next>Next</button></div>
<div id=s2 style="display:none"><input type=password id=pw><button id=signin>Sign In</button></div>
<div id=err></div>
<script>
document.getElementById('next').onclick = () => {{ document.getElementById('s1').style.display='none';
  document.getElementById('s2').style.display='block'; }};
document.getElementById('signin').onclick = () => {{
  if (document.getElementById('pw').value === '{PASSWORD}') {{ document.cookie='sess=1; path=/'; location.href='/dashboard'; }}
  else document.getElementById('err').innerText = 'Invalid password';
}};
</script></body></html>"""


class Site:
    """venues: {(acct, vid): (venue label, {iso: {name,status,hours,rows}})}"""

    def __init__(self, venues, start="2026-11-16", require_login=False):
        self.venues, self.start, self.require_login = venues, start, require_login
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                logged = "sess=1" in (self.headers.get("Cookie") or "")
                if path in ("/", "/login"):
                    return self._send(LOGIN_HTML)
                if path == "/dashboard":
                    return self._send("<html><body>Dashboard Eco-Accounts</body></html>")
                parts = path.strip("/").split("/")
                if len(parts) >= 5 and parts[0] == "2":
                    if site.require_login and not logged:
                        return self._send(LOGIN_HTML)
                    key = (parts[1], parts[2])
                    if key not in site.venues:
                        return self._send("<html><body>not found</body></html>", 404)
                    label, events = site.venues[key]
                    if len(parts) >= 6 and parts[4] == "dayview" and len(parts[5]) == 6:
                        y, m, d = int("20" + parts[5][:2]), int(parts[5][2:4]), int(parts[5][4:6])
                        night = date(y, m, d)
                        ev = events.get(night.isoformat())
                        if ev:
                            return self._send(event_html(label, ev, night))
                    return self._send(calendar_html(label, key[0], key[1], events, site.start))
                return self._send("<html><body>?</body></html>", 404)

            def _send(self, body, code=200):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"
