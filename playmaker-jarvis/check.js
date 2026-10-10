// Playmaker Jarvis: counts yesterday's TAO validations per event.
//
//   node check.js              headless check for yesterday
//   node check.js --debug      visible Edge window, slowed down so you can watch
//   node check.js --date=2026-10-09   check a specific event start date
import readline from "node:readline";
import {
  PATHS, ROOT, loadConfig, createLogger, acquireLock, releaseLock,
  writeJson, ymd, parseYmd, yesterday,
} from "./lib/common.js";
import {
  JarvisError, launchBrowser, watchNavigation, restoreSessionCookies, saveSessionState,
  detectAuth, openPromotingEvents, setEventStatusPast, applyFilters,
  collectEventsForDate, countEventValidations, screenshot,
} from "./lib/tao.js";

const args = process.argv.slice(2);
const debug = args.includes("--debug");
const mode = debug ? "debug" : args.includes("--scheduled") ? "scheduled" : "manual";
const dateArg = args.find((a) => a.startsWith("--date="))?.slice(7);

const config = loadConfig();
const log = createLogger("check");
const startedAt = new Date();
const target = dateArg ? parseYmd(dateArg) : yesterday();
let context = null;
let finished = false;

function baseReport(targetDate) {
  return {
    reportDate: targetDate ? ymd(targetDate) : null,
    checkedAt: startedAt.toISOString(),
    finishedAt: new Date().toISOString(),
    durationSeconds: Math.round((Date.now() - startedAt) / 1000),
    mode,
  };
}

function writeErrorReport(targetDate, error, screenshotFile) {
  try {
    writeJson(PATHS.latest, {
      ...baseReport(targetDate),
      status: "error",
      message: error.message,
      hint: error.hint ?? "Run Check-Now-Debug.bat to watch the workflow, then send logs\\jarvis-error.log.",
      screenshot: screenshotFile ?? null,
      events: [],
      total: 0,
    });
  } catch (writeError) {
    log.error("report", "Could not write the error report", writeError);
  }
}

async function closeBrowser() {
  if (!context) return;
  const c = context;
  context = null;
  let timer;
  await Promise.race([c.close().catch(() => {}), new Promise((r) => { timer = setTimeout(r, 10000); })]);
  clearTimeout(timer);
  log.info("browser", "Edge closed");
}

function waitForEnter(prompt) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    rl.question(prompt, () => { rl.close(); resolve(); });
  });
}

async function fatal(kind, error) {
  if (finished) return;
  finished = true;
  log.error("fatal", `${kind}: ${error?.message ?? error}`, error instanceof Error ? error : undefined);
  writeErrorReport(target, error instanceof Error ? error : new Error(String(error)));
  await closeBrowser();
  releaseLock();
  process.exit(1);
}

process.on("unhandledRejection", (reason) => fatal("Unhandled promise rejection", reason));
process.on("uncaughtException", (error) => fatal("Uncaught exception", error));

const watchdog = setTimeout(
  () => fatal("Timeout", new JarvisError("The check took longer than 20 minutes and was stopped.")),
  20 * 60 * 1000);

async function openFilteredList(page) {
  await page.goto(config.taoLoginUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
  const auth = await detectAuth(page, log, 45000);
  if (auth.state !== "signed-in") {
    throw new JarvisError(
      auth.state === "signed-out"
        ? "TAO is not signed in on the Jarvis Edge profile (the login form is showing)."
        : `Could not confirm the TAO sign-in (${auth.evidence}).`,
      "Double-click Start-Jarvis-Login.bat, sign in until you see \"Events I'm Promoting\", press ENTER, then run Check-Now.bat again.");
  }
  await openPromotingEvents(page, log);
  await setEventStatusPast(page, log);
  await applyFilters(page, log);
}

async function run() {
  log.info("start", `==== Jarvis check starting (${mode} mode, pid ${process.pid}, Node ${process.version}) ====`);
  log.info("start", `Project folder: ${ROOT}`);
  if (!target) throw new JarvisError(`Invalid --date value "${dateArg}". Use YYYY-MM-DD.`);
  log.info("start", `Counting validations for events that started on ${ymd(target)} (${target.toDateString()})`);

  const lock = acquireLock(mode);
  if (!lock.ok) {
    log.error("start", `Another Jarvis check is already running (pid ${lock.active?.pid}, started ${lock.active?.startedAt}). Not starting a second one.`);
    return 2;
  }

  let page = null;
  try {
    context = await launchBrowser(config, log, { visible: debug, slowMo: debug ? config.debugSlowMoMs : 0 });
    context.setDefaultTimeout(20000);
    context.setDefaultNavigationTimeout(45000);
    page = context.pages()[0] ?? await context.newPage();
    watchNavigation(page, log, "Main");
    await restoreSessionCookies(context, log);

    log.info("navigate", `Opening ${config.taoLoginUrl}`);
    await openFilteredList(page);

    const { matches, scanned } = await collectEventsForDate(page, log, target);
    log.info("events", `Found ${matches.length} event(s) that started on ${ymd(target)} out of ${scanned} past event(s) scanned`);

    const breakdown = [];
    for (const event of matches) {
      try {
        const result = await countEventValidations(context, page, log, event, openFilteredList);
        breakdown.push({ event: event.title, startDate: ymd(event.startDate), url: event.href, ...result });
      } catch (error) {
        log.error("sales", `Failed to count "${event.title}": ${error.message}`, error);
        breakdown.push({ event: event.title, startDate: ymd(event.startDate), url: event.href, validations: 0, error: error.message, screenshot: error.screenshot ?? null });
      }
    }

    const failed = breakdown.filter((b) => b.error).length;
    const notes = [];
    let status = "success";
    if (scanned === 0) {
      status = "warning";
      notes.push('No "View Sales" entries were found on the Past events list. TAO\'s layout may have changed.');
    } else if (matches.length === 0) {
      notes.push(`No events with a start date of ${ymd(target)} were found among ${scanned} past events.`);
    }
    if (failed) {
      status = failed === breakdown.length ? "error" : "partial";
      notes.push(`${failed} of ${breakdown.length} event(s) could not be counted.`);
    }

    await saveSessionState(context, log).catch((e) => log.warn("session", `Could not refresh session snapshot: ${e.message}`));

    const report = {
      ...baseReport(target),
      status,
      events: breakdown.sort((a, b) => b.validations - a.validations),
      total: breakdown.reduce((sum, b) => sum + (b.validations ?? 0), 0),
      pastEventsScanned: scanned,
      notes,
      message: status === "error" ? "None of yesterday's events could be counted." : undefined,
    };
    writeJson(PATHS.latest, report);
    if (status === "success" || status === "partial") {
      writeJson(`${PATHS.history}/${report.reportDate}.json`, report);
    }
    if (status === "success") writeJson(PATHS.lastSuccess, report);
    log.info("report", `Report written to ${PATHS.latest}: status ${status}, total ${report.total} validations across ${breakdown.length} event(s)`);
    for (const b of report.events) log.info("report", `  ${String(b.validations).padStart(5)}  ${b.event}${b.error ? "  (ERROR: " + b.error + ")" : ""}`);
    return status === "success" ? 0 : 1;
  } catch (error) {
    const shot = page ? await screenshot(page, log, "check-error") : null;
    log.error("check", error.message, error);
    if (error.hint) log.error("check", `What to do: ${error.hint}`);
    if (/Timeout \d+ms exceeded/i.test(error.message)) log.error("check", "A TAO page element did not appear in time. The screenshot shows where it stopped.");
    writeErrorReport(target, error, shot);
    if (debug && context) await waitForEnter("\nDebug mode: the Edge window is left open so you can see where Jarvis stopped. Press ENTER to close it...");
    return 1;
  }
}

const code = await run().catch((error) => {
  log.error("check", error.message, error);
  writeErrorReport(target, error);
  return 1;
});
finished = true;
clearTimeout(watchdog);
await closeBrowser();
releaseLock();
log.info("end", `==== Jarvis check finished with exit code ${code} after ${Math.round((Date.now() - startedAt) / 1000)}s ====`);
process.exitCode = code;
