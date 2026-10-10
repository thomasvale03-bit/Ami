// Shared paths, config, logging, locking, report files and date parsing.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

export const PATHS = {
  profile: path.join(ROOT, ".tao-session"),
  sessionState: path.join(ROOT, ".tao-session", "jarvis-session-state.json"),
  spotifyToken: path.join(ROOT, ".spotify-session", "token.json"),
  data: path.join(ROOT, "data"),
  history: path.join(ROOT, "data", "history"),
  latest: path.join(ROOT, "data", "latest.json"),
  lastSuccess: path.join(ROOT, "data", "last-success.json"),
  lock: path.join(ROOT, "data", "check.lock"),
  logs: path.join(ROOT, "logs"),
  screenshots: path.join(ROOT, "logs", "screenshots"),
  log: path.join(ROOT, "logs", "jarvis.log"),
  errorLog: path.join(ROOT, "logs", "jarvis-error.log"),
  config: path.join(ROOT, "config.json"),
};

const DEFAULT_CONFIG = {
  taoLoginUrl: "https://tickets.taogroup.com/login",
  scheduleTime: "10:00",
  dashboardPort: 8787,
  debugSlowMoMs: 250,
  spotifyClientId: "",
  voiceEnabled: true,
  voiceAddress: "sir",
  voiceName: "",
};

export function loadConfig() {
  let fromFile = {};
  try {
    fromFile = JSON.parse(fs.readFileSync(PATHS.config, "utf8"));
  } catch {
    // Missing or unreadable config.json: defaults are used.
  }
  const config = { ...DEFAULT_CONFIG, ...fromFile };
  // Test hooks (used only for development against a mock TAO site).
  if (process.env.JARVIS_TAO_URL) config.taoLoginUrl = process.env.JARVIS_TAO_URL;
  if (process.env.JARVIS_PORT) config.dashboardPort = Number(process.env.JARVIS_PORT);
  return config;
}

// ---------------------------------------------------------------- logging

const pad = (n, w = 2) => String(n).padStart(w, "0");

export function timestamp(d = new Date()) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

export function fileStamp(d = new Date()) {
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
}

function rotate(file, maxBytes = 5 * 1024 * 1024) {
  try {
    if (fs.statSync(file).size > maxBytes) fs.renameSync(file, `${file}.1`);
  } catch {
    // File does not exist yet.
  }
}

function safeAppend(file, text) {
  try {
    fs.appendFileSync(file, text);
  } catch (error) {
    console.error(`(could not write to ${file}: ${error.message})`);
  }
}

export function createLogger(component) {
  fs.mkdirSync(PATHS.logs, { recursive: true });
  rotate(PATHS.log);
  rotate(PATHS.errorLog);

  const write = (level, step, message, error) => {
    const line = `${timestamp()} [${level.padEnd(5)}] [${component}:${step}] ${message}`;
    const detail = error?.stack ? `\n    ${String(error.stack).split("\n").join("\n    ")}` : "";
    if (level === "ERROR") console.error(line + detail);
    else console.log(line);
    safeAppend(PATHS.log, line + detail + "\n");
    if (level === "ERROR") safeAppend(PATHS.errorLog, line + detail + "\n");
  };

  return {
    info: (step, message) => write("INFO", step, message),
    warn: (step, message) => write("WARN", step, message),
    error: (step, message, error) => write("ERROR", step, message, error),
  };
}

export function tailLog(lines = 15) {
  try {
    const text = fs.readFileSync(PATHS.log, "utf8");
    return text.trimEnd().split("\n").filter((l) => !l.startsWith("    ")).slice(-lines);
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------- lock

function pidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === "EPERM";
  }
}

export function readLock() {
  try {
    const lock = JSON.parse(fs.readFileSync(PATHS.lock, "utf8"));
    const ageMinutes = (Date.now() - new Date(lock.startedAt).valueOf()) / 60000;
    if (pidAlive(lock.pid) && ageMinutes < 30) return lock;
  } catch {
    // No lock or unreadable lock.
  }
  return null;
}

export function acquireLock(mode) {
  fs.mkdirSync(PATHS.data, { recursive: true });
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      fs.writeFileSync(PATHS.lock, JSON.stringify({ pid: process.pid, mode, startedAt: new Date().toISOString() }), { flag: "wx" });
      return { ok: true };
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      const active = readLock();
      if (active) return { ok: false, active };
      try { fs.unlinkSync(PATHS.lock); } catch { /* already gone */ }
    }
  }
  return { ok: false, active: null };
}

export function releaseLock() {
  try {
    const lock = JSON.parse(fs.readFileSync(PATHS.lock, "utf8"));
    if (lock.pid === process.pid) fs.unlinkSync(PATHS.lock);
  } catch {
    // Nothing to release.
  }
}

// ---------------------------------------------------------------- JSON files

export function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const text = JSON.stringify(value, null, 2);
  const tmp = `${file}.${process.pid}.tmp`;
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      fs.writeFileSync(tmp, text);
      fs.renameSync(tmp, file);
      return;
    } catch {
      // Windows can briefly lock a file the dashboard is reading; retry.
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 150);
    }
  }
  fs.writeFileSync(file, text);
  try { fs.unlinkSync(tmp); } catch { /* ignore */ }
}

export function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- dates

export function ymd(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function parseYmd(text) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text ?? "");
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return d.getDate() === Number(m[3]) ? d : null;
}

export function yesterday(now = new Date()) {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
}

export function sameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

const MONTH_INDEX = { jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11 };
const MON = "(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)";
const DATE_PATTERNS = [
  { re: new RegExp(`\\b${MON}\\.?\\s+(\\d{1,2})(?:st|nd|rd|th)?(?!\\d)(?:,?\\s+(\\d{4})(?!\\d))?`, "gi"), parts: (m) => [m[3], m[1], m[2]] },
  { re: new RegExp(`\\b(\\d{1,2})(?:st|nd|rd|th)?\\s+${MON}\\.?,?(?:\\s+(\\d{4})(?!\\d))?`, "gi"), parts: (m) => [m[3], m[2], m[1]] },
  { re: /\b(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)/g, parts: (m) => [m[1], Number(m[2]) - 1, m[3]] },
  { re: /\b(\d{1,2})\/(\d{1,2})\/(\d{4}|\d{2})(?!\d)/g, parts: (m) => [m[3], Number(m[1]) - 1, m[2]] },
];

/**
 * Finds calendar dates in free text, in the order they appear.
 * Dates without a year take the reference date's year (or the previous
 * year when that would put them more than 60 days in the future).
 */
export function findDatesInText(text, reference = new Date()) {
  const found = [];
  for (const { re, parts } of DATE_PATTERNS) {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(text ?? ""))) {
      let [year, month, day] = parts(m);
      if (typeof month === "string") month = MONTH_INDEX[month.slice(0, 3).toLowerCase()];
      day = Number(day);
      let y = year ? Number(year) : reference.getFullYear();
      if (y < 100) y += 2000;
      let d = new Date(y, month, day);
      if (d.getMonth() !== month || d.getDate() !== day) continue;
      if (!year && d - reference > 60 * 86400000) d = new Date(y - 1, month, day);
      found.push({ date: d, index: m.index, end: m.index + m[0].length, raw: m[0] });
    }
  }
  found.sort((a, b) => a.index - b.index || b.end - a.end);
  const result = [];
  for (const f of found) {
    if (result.length && f.index < result[result.length - 1].end) continue;
    result.push(f);
  }
  return result;
}

/**
 * Counts redeemed tickets in one "Redeemed Times" cell.
 * Every redemption time is one ticket; "Not Redeemed" is never counted;
 * any other non-empty value counts as one ticket.
 */
export function countRedeemedCell(cellText) {
  let redeemed = 0;
  let notRedeemed = 0;
  const unusual = [];
  const lines = String(cellText ?? "").split(/\r?\n|;|\|/).map((s) => s.trim()).filter(Boolean);
  for (const line of lines) {
    if (/not\s*redeemed/i.test(line)) { notRedeemed++; continue; }
    if (/^([-–—]+|n\/?a|none|null|0)$/i.test(line)) continue;
    const times = line.match(/(?<![\d:])\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp]\.?[Mm]\.?)?/g) ?? [];
    if (times.length) {
      redeemed += times.length;
    } else {
      redeemed += 1;
      unusual.push(line);
    }
  }
  return { redeemed, notRedeemed, unusual };
}
