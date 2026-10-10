// TAO portal automation helpers. Selectors rely on visible labels with
// several fallbacks so small TAO layout changes don't break the check.
import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright-core";
import { PATHS, fileStamp, findDatesInText, countRedeemedCell } from "./common.js";

export const RE = {
  promoting: /events\s*i\s*['’‘`´]?\s*a?m\s+promoting/i,
  viewSales: /view\s*sales/i,
  applyFilters: /apply\s*filters?/i,
  filtersToggle: /^\s*(show\s+)?filters?\s*$/i,
  logout: /\b(log\s*out|sign\s*out)\b/i,
  loginButton: /^\s*(log\s*in|sign\s*in|login|continue)\s*$/i,
  customers: /^\s*customers?(\s*\(\d+\))?\s*$/i,
  redeemed: /redeemed\s*times?/i,
  past: /^\s*past(\s+events?)?\s*$/i,
  eventStatus: /event\s*status/i,
  next: /^\s*(next|next\s*page|go\s+to\s+next\s+page|›|»|>|→)\s*$/i,
  noResults: /no\s+(customers|results|records|data|tickets)/i,
};

export class JarvisError extends Error {
  constructor(message, hint) {
    super(message);
    this.hint = hint;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const short = (e) => String(e?.message ?? e).split("\n")[0].slice(0, 200);

// ---------------------------------------------------------------- browser

export async function launchBrowser(config, log, { visible = false, slowMo = 0 } = {}) {
  fs.mkdirSync(PATHS.profile, { recursive: true });
  const options = {
    headless: !visible,
    slowMo,
    viewport: visible ? null : { width: 1600, height: 1000 },
    args: visible ? ["--start-maximized"] : [],
  };
  if (process.env.JARVIS_BROWSER_PATH) options.executablePath = process.env.JARVIS_BROWSER_PATH;
  else options.channel = "msedge";

  log.info("browser", `Launching Microsoft Edge (${visible ? "VISIBLE debug window" : "headless/background"})`);
  log.info("browser", `Persistent profile folder: ${PATHS.profile}`);
  try {
    const context = await chromium.launchPersistentContext(PATHS.profile, options);
    log.info("browser", `Edge launched (browser version ${context.browser()?.version() ?? "unknown"})`);
    return context;
  } catch (error) {
    const msg = String(error.message);
    if (/distribution 'msedge' is not found|executable doesn't exist|Failed to launch.*msedge/i.test(msg)) {
      throw new JarvisError("Microsoft Edge could not be found on this computer.",
        "Install or update Microsoft Edge from microsoft.com/edge, then run the check again.");
    }
    if (/ProcessSingleton|user data directory is already in use|SingletonLock|exitCode=21/i.test(msg)) {
      throw new JarvisError("The Jarvis Edge profile is already open in another window.",
        "Close every Edge window that Jarvis opened (for example the TAO login window) and try again.");
    }
    throw error;
  }
}

export function watchNavigation(page, log, label) {
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) log.info("navigate", `${label} URL: ${frame.url()}`);
  });
}

/** Adds saved cookies that the profile lost (TAO session cookies vanish when Edge closes). */
export async function restoreSessionCookies(context, log) {
  let state;
  try {
    state = JSON.parse(fs.readFileSync(PATHS.sessionState, "utf8"));
  } catch {
    log.warn("session", "No saved session snapshot found. Relying on the Edge profile only.");
    return;
  }
  const now = Date.now() / 1000;
  const existing = new Set((await context.cookies()).map((c) => `${c.name}|${c.domain}|${c.path}`));
  const missing = (state.cookies ?? []).filter((c) =>
    !existing.has(`${c.name}|${c.domain}|${c.path}`) && (c.expires === -1 || c.expires > now));
  if (missing.length) await context.addCookies(missing);
  log.info("session", `Session snapshot from ${state.savedAt ?? "unknown time"}: ${state.cookies?.length ?? 0} cookies, restored ${missing.length} missing`);
}

export async function saveSessionState(context, log) {
  const state = await context.storageState();
  state.savedAt = new Date().toISOString();
  fs.mkdirSync(path.dirname(PATHS.sessionState), { recursive: true });
  fs.writeFileSync(PATHS.sessionState, JSON.stringify(state));
  const tao = state.cookies.filter((c) => /taogroup/i.test(c.domain)).length;
  log.info("session", `Session snapshot saved (${state.cookies.length} cookies, ${tao} for TAO). No password is stored.`);
}

export async function screenshot(page, log, name) {
  try {
    fs.mkdirSync(PATHS.screenshots, { recursive: true });
    const file = path.join(PATHS.screenshots, `${name}-${fileStamp()}.png`);
    await page.screenshot({ path: file, fullPage: true, timeout: 10000 });
    log.info("screenshot", `Saved ${file}`);
    const old = fs.readdirSync(PATHS.screenshots).sort().reverse().slice(30);
    for (const f of old) fs.unlinkSync(path.join(PATHS.screenshots, f));
    return file;
  } catch (error) {
    log.warn("screenshot", `Could not save screenshot: ${short(error)}`);
    return null;
  }
}

// ---------------------------------------------------------------- generic helpers

async function firstVisible(candidates) {
  for (const [desc, locator] of candidates) {
    try {
      const count = await locator.count();
      for (let i = 0; i < Math.min(count, 10); i++) {
        const item = locator.nth(i);
        if (await item.isVisible()) return { desc, locator: item };
      }
    } catch {
      // Detached or invalid; try the next candidate.
    }
  }
  return null;
}

async function waitForFirstVisible(candidates, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  do {
    const hit = await firstVisible(candidates);
    if (hit) return hit;
    await sleep(500);
  } while (Date.now() < deadline);
  return null;
}

async function clickFirst(candidates, log, step, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  do {
    const hit = await firstVisible(candidates);
    if (hit) {
      try {
        await hit.locator.scrollIntoViewIfNeeded({ timeout: 3000 }).catch(() => {});
        await hit.locator.click({ timeout: 5000 });
        log.info(step, `Clicked ${hit.desc}`);
        return hit.desc;
      } catch (error) {
        lastError = error;
        log.warn(step, `Click on ${hit.desc} failed: ${short(error)}`);
      }
    }
    await sleep(500);
  } while (Date.now() < deadline);
  if (lastError) log.warn(step, `Giving up after ${timeoutMs / 1000}s: ${short(lastError)}`);
  return null;
}

async function settle(page, ms = 1500) {
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});
  await sleep(ms);
}

// ---------------------------------------------------------------- authentication

function authCandidates(page) {
  return {
    signedIn: [
      ['"Events I\'m Promoting" text', page.getByText(RE.promoting)],
      ['"View Sales" link', page.getByRole("link", { name: RE.viewSales })],
      ['"View Sales" button', page.getByRole("button", { name: RE.viewSales })],
      ['"Apply Filters" button', page.getByRole("button", { name: RE.applyFilters })],
      ['"Log out" control', page.getByRole("link", { name: RE.logout })],
      ['"Log out" button', page.getByRole("button", { name: RE.logout })],
    ],
    signedOut: [
      ["password field", page.locator('input[type="password"]')],
      ['"Log in" button', page.getByRole("button", { name: RE.loginButton })],
    ],
  };
}

/**
 * Decides whether TAO is signed in from what is visible on the page,
 * never from the URL (TAO can keep /login in the address bar after sign-in).
 * Returns { state: "signed-in" | "signed-out" | "unknown", evidence }.
 */
export async function detectAuth(page, log, timeoutMs = 45000) {
  const deadline = Date.now() + timeoutMs;
  const signedOutSince = { at: null };
  let lastNote = "";
  while (true) {
    const { signedIn, signedOut } = authCandidates(page);
    const yes = await firstVisible(signedIn);
    if (yes) {
      log.info("auth", `Signed in: found ${yes.desc} (URL ${page.url()})`);
      return { state: "signed-in", evidence: yes.desc };
    }
    const no = await firstVisible(signedOut);
    const note = no ? `login form visible (${no.desc})` : "neither login form nor promoter page visible yet";
    if (note !== lastNote) {
      log.info("auth", `Waiting: ${note} (URL ${page.url()})`);
      lastNote = note;
    }
    if (no) {
      signedOutSince.at ??= Date.now();
      // Give TAO time to redirect a valid session away from the login form.
      if (Date.now() - signedOutSince.at > 15000) return { state: "signed-out", evidence: no.desc };
    } else {
      signedOutSince.at = null;
    }
    if (Date.now() > deadline) return { state: no ? "signed-out" : "unknown", evidence: note };
    await sleep(1000);
  }
}

// ---------------------------------------------------------------- events list

export async function openPromotingEvents(page, log) {
  const applyVisible = () => firstVisible([["Apply Filters", page.getByRole("button", { name: RE.applyFilters })], ["Apply Filters text", page.getByText(RE.applyFilters)]]);
  if (await applyVisible()) {
    log.info("events", '"Events I\'m Promoting" is already open');
    return;
  }
  const clicked = await clickFirst([
    ['"Events I\'m Promoting" link', page.getByRole("link", { name: RE.promoting })],
    ['"Events I\'m Promoting" tab', page.getByRole("tab", { name: RE.promoting })],
    ['"Events I\'m Promoting" button', page.getByRole("button", { name: RE.promoting })],
    ['"Events I\'m Promoting" menu item', page.getByRole("menuitem", { name: RE.promoting })],
    ['"Events I\'m Promoting" text', page.getByText(RE.promoting)],
  ], log, "events", 10000);
  if (!clicked) log.warn("events", 'Could not click "Events I\'m Promoting"; assuming it is the current page');
  await settle(page);

  if (await waitForFirstVisible([["Apply Filters", page.getByRole("button", { name: RE.applyFilters })], ["Apply Filters text", page.getByText(RE.applyFilters)]], 15000)) return;
  log.info("events", '"Apply Filters" not visible; trying to open a Filters panel');
  await clickFirst([
    ["Filters button", page.getByRole("button", { name: RE.filtersToggle })],
    ["Filters text", page.getByText(RE.filtersToggle)],
  ], log, "events", 5000);
  if (!(await waitForFirstVisible([["Apply Filters", page.getByRole("button", { name: RE.applyFilters })], ["Apply Filters text", page.getByText(RE.applyFilters)]], 10000))) {
    throw new JarvisError('Could not find the "Apply Filters" button on "Events I\'m Promoting".',
      "Run Check-Now-Debug.bat to watch where it stops, and send the screenshot from logs\\screenshots.");
  }
}

async function chooseVisibleOption(page, log) {
  return clickFirst([
    ['"Past" option', page.getByRole("option", { name: RE.past })],
    ['"Past" menu item', page.getByRole("menuitem", { name: RE.past })],
    ['"Past" list item', page.locator("li, [role=option], [class*=option i], [class*=item i]").filter({ hasText: RE.past })],
    ['"Past" text', page.getByText(RE.past)],
  ], log, "filter", 5000);
}

export async function setEventStatusPast(page, log) {
  // 1. A native <select> labelled "Event Status", or any select offering "Past".
  const selectIndex = await page.evaluate(() => {
    const isVisible = (el) => el.getClientRects().length > 0;
    const selects = [...document.querySelectorAll("select")];
    const scored = selects.map((s, i) => {
      const opt = [...s.options].findIndex((o) => /^\s*past(\s+events?)?\s*$/i.test(o.textContent));
      const label = (s.labels?.[0]?.textContent ?? "") + " " + (s.getAttribute("aria-label") ?? "") + " " + (s.name ?? "") + " " + (s.id ?? "") +
        " " + (s.closest("label,div,fieldset")?.textContent ?? "").slice(0, 200);
      return { i, opt, status: /status/i.test(label), visible: isVisible(s) };
    }).filter((s) => s.opt >= 0);
    scored.sort((a, b) => Number(b.status) - Number(a.status) || Number(b.visible) - Number(a.visible));
    return scored[0] ?? null;
  });
  if (selectIndex) {
    try {
      await page.locator("select").nth(selectIndex.i).selectOption({ index: selectIndex.opt }, { timeout: 5000 });
      log.info("filter", `Event Status set to "Past" using dropdown #${selectIndex.i + 1}${selectIndex.status ? " (labelled Status)" : ""}`);
      return;
    } catch (error) {
      log.warn("filter", `Native dropdown selection failed: ${short(error)}`);
    }
  }

  // 2. A custom dropdown labelled "Event Status".
  const opened = await clickFirst([
    ['"Event Status" combobox', page.getByRole("combobox", { name: RE.eventStatus })],
    ['"Event Status" button', page.getByRole("button", { name: RE.eventStatus })],
    ["control after \"Event Status\" label", page.getByText(RE.eventStatus).locator("xpath=following::*[self::button or self::input or @role='combobox' or @role='button' or contains(@class,'select')][1]")],
    ['"Event Status" label', page.getByText(RE.eventStatus)],
  ], log, "filter", 8000);
  if (opened && await chooseVisibleOption(page, log)) {
    log.info("filter", 'Event Status set to "Past" using custom dropdown');
    return;
  }
  if (opened) await page.keyboard.press("Escape").catch(() => {});

  // 3. Radio buttons, tabs or toggle buttons named "Past".
  const direct = await firstVisible([
    ['"Past" radio', page.getByRole("radio", { name: RE.past })],
    ['"Past" checkbox', page.getByRole("checkbox", { name: RE.past })],
    ['"Past" tab', page.getByRole("tab", { name: RE.past })],
    ['"Past" button', page.getByRole("button", { name: RE.past })],
  ]);
  if (direct) {
    await direct.locator.click({ timeout: 5000 });
    log.info("filter", `Event Status set to "Past" using ${direct.desc}`);
    return;
  }
  throw new JarvisError('Could not set "Event Status" to "Past".',
    "Run Check-Now-Debug.bat to watch the filter step, and send the screenshot from logs\\screenshots.");
}

export async function applyFilters(page, log) {
  const clicked = await clickFirst([
    ['"Apply Filters" button', page.getByRole("button", { name: RE.applyFilters })],
    ['"Apply Filters" text', page.getByText(RE.applyFilters)],
  ], log, "filter", 10000);
  if (!clicked) throw new JarvisError('Could not click "Apply Filters".');
  await settle(page, 2500);
  log.info("filter", `Filters applied (URL ${page.url()})`);
}

/** Tags every visible "View Sales" control and returns the text of its event row. */
async function scanEventRows(page) {
  return page.evaluate(() => {
    const vs = /view\s*sales/i;
    const dateRe = /\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}\b|\b\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)|\b\d{4}-\d{1,2}-\d{1,2}|\b\d{1,2}\/\d{1,2}\/\d{2,4}/i;
    const textOf = (el) => (el.innerText ?? el.textContent ?? "").trim();
    let controls = [...document.querySelectorAll("a, button, [role=button], [role=link]")]
      .filter((el) => vs.test(textOf(el)) && textOf(el).length < 40 && el.getClientRects().length > 0);
    controls = controls.filter((el) => !controls.some((other) => other !== el && el.contains(other)));
    const countVs = (el) => (textOf(el).match(/view\s*sales/gi) ?? []).length;

    const precedingDate = (row) => {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      walker.currentNode = row;
      for (let i = 0; i < 300 && walker.previousNode(); i++) {
        const t = walker.currentNode.textContent.trim();
        if (t && dateRe.test(t)) return t.slice(0, 120);
      }
      return "";
    };

    return controls.map((el, idx) => {
      el.setAttribute("data-jarvis-view-sales", String(idx));
      let row = el;
      let node = el.parentElement;
      while (node && node !== document.body && countVs(node) <= 1) {
        row = node;
        if (dateRe.test(textOf(node)) && textOf(node).length > 15) break;
        node = node.parentElement;
      }
      const text = textOf(row).slice(0, 1500);
      const href = el.tagName === "A" ? el.href : (el.closest("a")?.href ?? el.getAttribute("data-href") ?? null);
      return { idx, text, href, contextDate: dateRe.test(text) ? "" : precedingDate(row) };
    });
  });
}

function eventTitle(text) {
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  const skip = /view\s*sales|tracking\s*links?|copy|share|edit|^tickets?\b|^sales\b|^status\b|^\$|^\d+(\.\d+)?%?$|^(past|upcoming|live|active|ended)$/i;
  return lines.find((l) => !skip.test(l) && findDatesInText(l).length === 0 && !/^\d{1,2}:\d{2}/.test(l)) ??
    lines.find((l) => !skip.test(l)) ?? "Untitled event";
}

async function findNextControl(page) {
  const hit = await firstVisible([
    ['"Next" button', page.getByRole("button", { name: RE.next })],
    ['"Next" link', page.getByRole("link", { name: RE.next })],
    ['"Next" title', page.locator('[title="Next" i], [title="Next page" i]')],
    ['"Next" text', page.getByText(/^\s*next\s*$/i)],
  ]);
  if (!hit) return null;
  const disabled = await hit.locator.evaluate((el) =>
    el.disabled === true ||
    el.getAttribute("aria-disabled") === "true" ||
    /\bdisabled\b/i.test(el.getAttribute("class") ?? "") ||
    !!el.closest('[aria-disabled="true"], .disabled, [disabled]')).catch(() => true);
  return disabled ? null : hit;
}

/** Walks the (possibly paginated) past-events list and returns events that started on targetDate. */
export async function collectEventsForDate(page, log, targetDate) {
  const matches = [];
  const seenPages = new Set();
  let scanned = 0;
  for (let pageNo = 1; pageNo <= 25; pageNo++) {
    const rows = await scanEventRows(page);
    const signature = rows.map((r) => r.text).join("\u0001");
    if (seenPages.has(signature)) break;
    seenPages.add(signature);
    log.info("events", `Events list page ${pageNo}: ${rows.length} "View Sales" entries`);

    let oldest = null;
    let newest = null;
    for (const row of rows) {
      scanned++;
      const dates = findDatesInText(row.text, targetDate);
      const contextDates = row.contextDate ? findDatesInText(row.contextDate, targetDate) : [];
      const start = dates[0]?.date ?? contextDates[0]?.date ?? null;
      const title = eventTitle(row.text);
      if (!start) {
        log.warn("events", `No date found for "${title}" (row text: ${row.text.replace(/\s+/g, " ").slice(0, 160)})`);
        continue;
      }
      if (!oldest || start < oldest) oldest = start;
      if (!newest || start > newest) newest = start;
      const isTarget = start.getFullYear() === targetDate.getFullYear() && start.getMonth() === targetDate.getMonth() && start.getDate() === targetDate.getDate();
      log.info("events", `${isTarget ? "MATCH " : "skip  "} start ${start.toDateString()} | ${title}`);
      if (isTarget) matches.push({ title, startDate: start, href: row.href, rowText: row.text, page: pageNo });
    }

    // Lists sorted newest-first can stop once they are older than the target date.
    const firstDate = rows.length ? findDatesInText(rows[0].text, targetDate)[0]?.date : null;
    const lastDate = rows.length ? findDatesInText(rows[rows.length - 1].text, targetDate)[0]?.date : null;
    if (firstDate && lastDate && firstDate >= lastDate && newest && newest < targetDate) {
      log.info("events", "Remaining pages are older than the target date; stopping");
      break;
    }

    const next = await findNextControl(page);
    if (!next) break;
    log.info("events", `Opening next events page via ${next.desc}`);
    await next.locator.click({ timeout: 5000 });
    await settle(page, 1500);
  }
  return { matches, scanned };
}

// ---------------------------------------------------------------- sales / customers

async function openSalesPage(context, page, log, event, reopenList) {
  if (event.href && /^https?:/i.test(event.href)) {
    const detail = await context.newPage();
    watchNavigation(detail, log, "View Sales");
    log.info("sales", `Opening View Sales for "${event.title}": ${event.href}`);
    await detail.goto(event.href, { waitUntil: "domcontentloaded", timeout: 45000 });
    return detail;
  }
  // "View Sales" is a button without a link: re-open the filtered list and click it.
  log.info("sales", `"View Sales" for "${event.title}" is a button; clicking it from the events list`);
  const listPage = await context.newPage();
  watchNavigation(listPage, log, "View Sales");
  await reopenList(listPage);
  for (let pageNo = 1; pageNo < event.page; pageNo++) {
    const next = await findNextControl(listPage);
    if (!next) break;
    await next.locator.click();
    await settle(listPage, 1500);
  }
  const rows = await scanEventRows(listPage);
  const row = rows.find((r) => r.text === event.rowText) ?? rows.find((r) => eventTitle(r.text) === event.title);
  if (!row) throw new JarvisError(`Could not find "${event.title}" again in the events list.`);
  const before = listPage.url();
  const popup = context.waitForEvent("page", { timeout: 8000 }).catch(() => null);
  await listPage.locator(`[data-jarvis-view-sales="${row.idx}"]`).click();
  const newPage = await popup;
  if (newPage) {
    await listPage.close();
    await newPage.waitForLoadState("domcontentloaded");
    return newPage;
  }
  await listPage.waitForURL((u) => u.toString() !== before, { timeout: 15000 }).catch(() => {});
  return listPage;
}

async function readCustomerTable(page) {
  return page.evaluate(() => {
    const R = /redeemed\s*times?/i;
    const textOf = (el) => (el.innerText ?? el.textContent ?? "").trim();
    const visible = (el) => el.getClientRects().length > 0;
    const header = [...document.querySelectorAll("th, [role=columnheader], thead td")].find((h) => R.test(textOf(h)) && visible(h));
    if (!header) return { found: false };
    const headRow = header.closest("tr, [role=row]");
    const cellsOf = (row) => [...row.children].filter((c) => c.matches("th, td, [role=cell], [role=gridcell], [role=columnheader], [role=rowheader]"));
    let col = 0;
    for (const cell of cellsOf(headRow)) {
      if (cell === header || cell.contains(header)) break;
      col += Number(cell.getAttribute("colspan") ?? 1) || 1;
    }
    const cellAt = (row, logical) => {
      let pos = 0;
      for (const cell of cellsOf(row)) {
        const span = Number(cell.getAttribute("colspan") ?? 1) || 1;
        if (logical < pos + span) return cell;
        pos += span;
      }
      return null;
    };
    const table = header.closest("table, [role=table], [role=grid], [role=treegrid]") ?? headRow.parentElement?.parentElement ?? document.body;
    const rows = [...table.querySelectorAll("tr, [role=row]")].filter((r) =>
      r !== headRow && visible(r) && !r.querySelector("th, [role=columnheader]") && cellsOf(r).length > 1);
    const cells = rows.map((r) => textOf(cellAt(r, col) ?? document.createElement("td")));
    // Hash of the whole table text so pagination can detect page changes (no customer data leaves the page).
    let hash = 0;
    for (const ch of rows.map(textOf).join("\u0001")) hash = (hash * 31 + ch.charCodeAt(0)) | 0;
    return { found: true, cells, rowCount: rows.length, hash: String(hash) };
  });
}

export async function countEventValidations(context, page, log, event, reopenList) {
  const detail = await openSalesPage(context, page, log, event, reopenList);
  try {
    await settle(detail, 1000);
    const heading = await detail.locator("h1, h2").first().innerText({ timeout: 3000 }).catch(() => "");
    if (heading) log.info("sales", `Sales page heading: ${heading.trim().slice(0, 120)}`);

    const customersCandidates = [
      ['"Customers" tab', detail.getByRole("tab", { name: RE.customers })],
      ['"Customers" link', detail.getByRole("link", { name: RE.customers })],
      ['"Customers" button', detail.getByRole("button", { name: RE.customers })],
      ['"Customers" text', detail.getByText(RE.customers)],
    ];
    const headerCandidates = [
      ['"Redeemed Times" column header', detail.getByRole("columnheader", { name: RE.redeemed })],
      ['"Redeemed Times" text', detail.getByText(RE.redeemed)],
    ];
    let header = null;
    for (let attempt = 1; attempt <= 2 && !header; attempt++) {
      const clicked = await clickFirst(customersCandidates, log, "customers", 20000);
      if (!clicked) throw new JarvisError(`Could not find the "Customers" tab for "${event.title}".`);
      await settle(detail, 1200);
      header = await waitForFirstVisible([...headerCandidates, ["no-customers message", detail.getByText(RE.noResults)]], 20000);
      if (header?.desc === "no-customers message") header = null;
      if (!header) {
        if (await detail.getByText(RE.noResults).first().isVisible().catch(() => false)) {
          log.info("customers", `"${event.title}": no customers listed; 0 validations`);
          return { validations: 0, notRedeemed: 0, ticketRows: 0, pages: 1 };
        }
        log.warn("customers", `"Redeemed Times" column not visible after attempt ${attempt}`);
      }
    }
    if (!header) throw new JarvisError(`The Customers tab for "${event.title}" has no "Redeemed Times" column.`);
    log.info("customers", `Customers tab open; found ${header.desc}`);

    let validations = 0;
    let notRedeemed = 0;
    let ticketRows = 0;
    const unusual = [];
    const seen = new Set();
    let pages = 0;
    for (let pageNo = 1; pageNo <= 300; pageNo++) {
      const table = await readCustomerTable(detail);
      if (!table.found) throw new JarvisError(`Lost the customer table on page ${pageNo} for "${event.title}".`);
      if (seen.has(table.hash)) break;
      seen.add(table.hash);
      pages++;
      let pageRedeemed = 0;
      let pageNot = 0;
      for (const cell of table.cells) {
        const r = countRedeemedCell(cell);
        pageRedeemed += r.redeemed;
        pageNot += r.notRedeemed;
        unusual.push(...r.unusual);
      }
      validations += pageRedeemed;
      notRedeemed += pageNot;
      ticketRows += table.rowCount;
      log.info("count", `"${event.title}" customers page ${pageNo}: ${table.rowCount} rows, ${pageRedeemed} redeemed tickets, ${pageNot} not redeemed`);

      const next = await findNextControl(detail);
      if (!next) break;
      await next.locator.click({ timeout: 5000 });
      const deadline = Date.now() + 10000;
      let changed = false;
      while (Date.now() < deadline) {
        await sleep(500);
        const after = await readCustomerTable(detail);
        if (after.found && after.hash !== table.hash) { changed = true; break; }
      }
      if (!changed) {
        log.info("count", "Next page did not change the table; treating this as the last page");
        break;
      }
    }
    if (unusual.length) {
      log.warn("count", `${unusual.length} "Redeemed Times" values had no time and were each counted as 1 ticket, e.g. ${JSON.stringify(unusual.slice(0, 5))}`);
    }
    log.info("count", `"${event.title}": ${validations} validations total (${notRedeemed} not redeemed, ${pages} page(s))`);
    return { validations, notRedeemed, ticketRows, pages, unusualValues: unusual.length };
  } catch (error) {
    error.screenshot = await screenshot(detail, log, "sales-error");
    throw error;
  } finally {
    await detail.close().catch(() => {});
  }
}
