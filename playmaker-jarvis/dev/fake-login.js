// Dev-only: simulates the manual sign-in against the mock portal.
import { loadConfig, createLogger } from "../lib/common.js";
import { launchBrowser, saveSessionState } from "../lib/tao.js";
const log = createLogger("fake-login");
const context = await launchBrowser(loadConfig(), log);
const page = context.pages()[0] ?? await context.newPage();
await page.goto(new URL("/do-login", process.env.JARVIS_TAO_URL).href);
await saveSessionState(context, log);
await context.close();
