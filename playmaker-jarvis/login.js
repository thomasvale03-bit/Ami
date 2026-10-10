// One-time (or renewal) TAO sign-in. You type your credentials into Edge
// yourself; Jarvis never sees or stores your password or verification code.
import readline from "node:readline";
import { PATHS, loadConfig, createLogger } from "./lib/common.js";
import { launchBrowser, watchNavigation, detectAuth, saveSessionState } from "./lib/tao.js";

const config = loadConfig();
const log = createLogger("login");
let context = null;
let windowClosed = false;

const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
const ask = (q) => new Promise((resolve) => rl.question(q, resolve));

async function signedInPage() {
  for (const page of [...context.pages()].reverse()) {
    const auth = await detectAuth(page, log, 3000);
    if (auth.state === "signed-in") return page;
  }
  return null;
}

async function main() {
  log.info("start", "==== TAO sign-in helper starting ====");
  context = await launchBrowser(config, log, { visible: true });
  context.on("close", () => {
    windowClosed = true;
    if (context) log.warn("browser", "The Jarvis Edge window was closed");
  });
  const page = context.pages()[0] ?? await context.newPage();
  watchNavigation(page, log, "Login");
  await page.goto(config.taoLoginUrl, { waitUntil: "domcontentloaded", timeout: 45000 });

  console.log("\n============================================================");
  console.log(" 1. Sign in to TAO in the Edge window that just opened.");
  console.log("    Type your password and code in EDGE, never in this window.");
  console.log(" 2. Wait until you can see \"Events I'm Promoting\".");
  console.log(" 3. Come back to this window and press ENTER.");
  console.log("============================================================\n");

  while (true) {
    const answer = (await ask("Press ENTER when \"Events I'm Promoting\" is showing (or type SAVE to save anyway): ")).trim().toUpperCase();
    if (windowClosed) throw new Error("The Edge window was closed before the session was saved. Run Start-Jarvis-Login.bat again.");
    const ok = await signedInPage();
    if (ok || answer === "SAVE") {
      if (!ok) log.warn("auth", "Saving without seeing \"Events I'm Promoting\" because you typed SAVE");
      await saveSessionState(context, log);
      break;
    }
    console.log("\nJarvis cannot see the \"Events I'm Promoting\" screen yet. Finish signing in, then press ENTER again.\n");
  }

  const c = context;
  context = null;
  await c.close();
  log.info("done", `TAO session saved in ${PATHS.profile}`);
  console.log("\nSecure TAO session saved on this computer.");
  console.log("Next step: double-click Check-Now-Debug.bat to watch Jarvis run the check.\n");
}

try {
  await main();
} catch (error) {
  log.error("login", error.message, error);
  if (error.hint) log.error("login", `What to do: ${error.hint}`);
  process.exitCode = 1;
} finally {
  rl.close();
  if (context && !windowClosed) await context.close().catch(() => {});
}
