// Development-only mock of the TAO promoter portal used to test Jarvis
// end to end. Not part of the Windows ZIP.
//   node dev/mock-tao.js   -> http://127.0.0.1:9911/login
import http from "node:http";

const EVENTS = [
  { id: "d", title: "TAO Beach Dayclub Saturday", when: "Sat, Oct 10, 2026 11:00 AM" , link: true },
  { id: "a", title: "JEWEL Fridays", when: "Fri, Oct 9, 2026 10:30 PM \u2013 Sat, Oct 10, 2026 4:00 AM", link: true },
  { id: "b", title: "Hakkasan \u2014 Ti\u00ebsto", when: "Fri, Oct 9, 2026 10:00 PM", link: false },
  { id: "c", title: "OMNIA Thursday", when: "Thu, Oct 8, 2026 10:30 PM", link: true },
  { id: "e", title: "Marquee Late Night", when: "Fri, Oct 9, 2026 11:30 PM", link: true },
  { id: "f", title: "LAVO Brunch", when: "Wed, Oct 7, 2026 12:00 PM", link: true },
];
const CUSTOMERS = {
  a: [
    ["Ana R", "Oct 9, 2026, 11:42 PM"],
    ["Ben S", "Not Redeemed"],
    ["Cara T", "Oct 9, 2026, 11:50 PM\nOct 9, 2026, 11:51 PM"],
    ["Dee U", "Oct 10, 2026, 12:30 AM\nNot Redeemed"],
    ["Eli V", "Oct 10, 2026, 1:02 AM"],
    ["Fay W", "Not Redeemed"],
  ],
  b: [["Gus X", "Oct 9, 2026, 11:05 PM\nOct 9, 2026, 11:06 PM"], ["Hal Y", "Not Redeemed"], ["Ivy Z", "Oct 9, 2026, 11:59 PM"]],
  e: [],
};

const page = (body) => `<!doctype html><html><head><meta charset="utf-8"><title>TAO mock</title></head><body>${body}</body></html>`;
const signedIn = (req) => /(^|;\s*)sess=ok/.test(req.headers.cookie ?? "");

http.createServer((req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");
  const html = (b) => { res.writeHead(200, { "Content-Type": "text/html" }); res.end(page(b)); };

  if (url.pathname === "/do-login") {
    // Session cookie (no expiry), like many portals use.
    res.writeHead(302, { "Set-Cookie": "sess=ok; Path=/; HttpOnly", Location: "/login" });
    return res.end();
  }
  if (url.pathname === "/login") {
    if (!signedIn(req)) {
      return html(`<h1>Sign in</h1><form action="/do-login"><input name="u"><input type="password" name="p"><button type="submit">Log In</button></form>`);
    }
    // Same /login URL after sign-in, curly apostrophe, nav link.
    return html(`<nav><a href="/events">Events I\u2019m Promoting</a> <a href="/logout">Log out</a></nav><h1>Welcome back</h1>`);
  }
  if (url.pathname === "/events") {
    if (!signedIn(req)) { res.writeHead(302, { Location: "/login" }); return res.end(); }
    return html(`
      <h1>Events I\u2019m Promoting</h1>
      <div><span>Event Status</span>
        <div class="select" role="combobox" tabindex="0" id="status" onclick="document.getElementById('opts').style.display='block'">Upcoming</div>
        <ul id="opts" style="display:none"><li role="option" onclick="pick('Upcoming')">Upcoming</li><li role="option" onclick="pick('Past')">Past</li></ul>
      </div>
      <button onclick="apply(1)">Apply Filters</button>
      <div id="list"></div>
      <script>
        let status='Upcoming';
        const EVENTS=${JSON.stringify(EVENTS)};
        function pick(s){status=s;document.getElementById('status').textContent=s;document.getElementById('opts').style.display='none';}
        function apply(p){
          const list=document.getElementById('list');
          if(status!=='Past'){list.innerHTML='<p>No upcoming events</p>';return;}
          const rows=EVENTS.slice((p-1)*4,p*4);
          list.innerHTML=rows.map(e=>'<div class="card"><h3>'+e.title+'</h3><p>'+e.when+'</p>'+
            (e.link?'<a href="/sales/'+e.id+'">View Sales</a>':'<button onclick="location.href=\\'/sales/'+e.id+'\\'">View Sales</button>')+
            ' <a href="#">Tracking Links</a></div>').join('')+
            '<div class="pager"><button '+(p===1?'disabled':'')+' onclick="apply('+(p-1)+')">Previous</button>'+
            '<button '+(p*4>=EVENTS.length?'disabled':'')+' onclick="apply('+(p+1)+')">Next</button></div>';
        }
      </script>`);
  }
  const m = /^\/sales\/(\w)$/.exec(url.pathname);
  if (m) {
    if (!signedIn(req)) { res.writeHead(302, { Location: "/login" }); return res.end(); }
    const ev = EVENTS.find((e) => e.id === m[1]);
    const rows = CUSTOMERS[m[1]] ?? [];
    return html(`
      <h1>${ev.title}</h1><p>${ev.when}</p>
      <div role="tablist"><button role="tab">Overview</button><button role="tab" onclick="show(1)">Customers</button></div>
      <div id="c"></div>
      <script>
        const ROWS=${JSON.stringify(rows)};
        function show(p){
          const c=document.getElementById('c');
          if(!ROWS.length){c.innerHTML='<p>No customers found</p>';return;}
          const slice=ROWS.slice((p-1)*4,p*4);
          c.innerHTML='<table><thead><tr><th>Name</th><th>Tickets</th><th>Time of Purchase</th><th>Redeemed Times</th><th>Actions</th></tr></thead><tbody>'+
            slice.map(r=>'<tr><td>'+r[0]+'</td><td>1</td><td>Oct 1, 2026, 3:00 PM</td><td>'+r[1].replace(/\\n/g,'<br>')+'</td><td><a href="#">Resend</a></td></tr>').join('')+
            '</tbody></table><nav><button aria-label="Next page" '+(p*4>=ROWS.length?'disabled':'')+' onclick="show('+(p+1)+')">\u203a</button></nav>';
        }
      </script>`);
  }
  res.writeHead(404); res.end("not found");
}).listen(9911, "127.0.0.1", () => console.log("mock TAO on http://127.0.0.1:9911/login"));
