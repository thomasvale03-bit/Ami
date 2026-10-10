// Jarvis dashboard server: http://127.0.0.1:8787 (local computer only).
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { spawn } from "node:child_process";
import { ROOT, PATHS, loadConfig, createLogger, readJson, writeJson, readLock, tailLog } from "./lib/common.js";

const config = loadConfig();
const log = createLogger("dashboard");
const port = config.dashboardPort;
const origin = `http://127.0.0.1:${port}`;

// ---------------------------------------------------------------- Spotify
// Authorization Code + PKCE. The refresh token is saved in .spotify-session
// on this computer so you stay signed in; your Spotify password never
// passes through Jarvis.

const SPOTIFY_SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing";
const spotifyRedirect = `${origin}/spotify/callback`;
const pendingLogins = new Map();

async function spotifyTokenRequest(params) {
  const res = await fetch("https://accounts.spotify.com/api/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: config.spotifyClientId, ...params }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`Spotify token error: ${body.error_description ?? body.error ?? res.status}`);
  return body;
}

function saveSpotifyToken(body, previous = {}) {
  const token = {
    accessToken: body.access_token,
    refreshToken: body.refresh_token ?? previous.refreshToken,
    expiresAt: Date.now() + (body.expires_in ?? 3600) * 1000 - 60000,
  };
  writeJson(PATHS.spotifyToken, token);
  return token;
}

async function spotifyAccessToken() {
  const token = readJson(PATHS.spotifyToken);
  if (!token?.refreshToken) return null;
  if (token.accessToken && Date.now() < token.expiresAt) return token.accessToken;
  try {
    const body = await spotifyTokenRequest({ grant_type: "refresh_token", refresh_token: token.refreshToken });
    return saveSpotifyToken(body, token).accessToken;
  } catch (error) {
    log.error("spotify", `Could not refresh Spotify sign-in: ${error.message}`);
    if (/invalid_grant|revoked/i.test(error.message)) fs.rmSync(PATHS.spotifyToken, { force: true });
    return null;
  }
}

async function spotifyApi(method, apiPath) {
  const access = await spotifyAccessToken();
  if (!access) return { status: 401, body: { error: "not_connected" } };
  const res = await fetch(`https://api.spotify.com/v1${apiPath}`, {
    method,
    headers: { Authorization: `Bearer ${access}` },
    body: method === "GET" ? undefined : "",
  });
  const text = await res.text();
  let body = {};
  try { body = text ? JSON.parse(text) : {}; } catch { body = { raw: text }; }
  return { status: res.status, body };
}

async function spotifyNowPlaying() {
  if (!config.spotifyClientId) return { configured: false };
  if (!readJson(PATHS.spotifyToken)?.refreshToken) return { configured: true, connected: false };
  const { status, body } = await spotifyApi("GET", "/me/player?additional_types=episode");
  if (status === 401) return { configured: true, connected: false };
  if (status === 204 || !body.item) return { configured: true, connected: true, active: false, device: body.device?.name ?? null };
  if (status >= 400) return { configured: true, connected: true, error: body.error?.message ?? `Spotify error ${status}` };
  const item = body.item;
  return {
    configured: true,
    connected: true,
    active: true,
    isPlaying: body.is_playing,
    title: item.name,
    artist: (item.artists ?? []).map((a) => a.name).join(", ") || item.show?.name || "",
    album: item.album?.name ?? item.show?.name ?? "",
    art: item.album?.images?.[0]?.url ?? item.images?.[0]?.url ?? null,
    progressMs: body.progress_ms,
    durationMs: item.duration_ms,
    device: body.device?.name ?? null,
  };
}

const SPOTIFY_CONTROLS = {
  play: ["PUT", "/me/player/play"],
  pause: ["PUT", "/me/player/pause"],
  next: ["POST", "/me/player/next"],
  previous: ["POST", "/me/player/previous"],
};

// ---------------------------------------------------------------- Check Now

function startCheck() {
  if (readLock()) return { started: false, message: "A check is already running." };
  const child = spawn(process.execPath, [path.join(ROOT, "check.js"), "--manual"], {
    cwd: ROOT, windowsHide: true, stdio: "ignore", detached: false,
  });
  child.on("exit", (code) => log.info("check", `Check Now finished with exit code ${code}`));
  child.on("error", (error) => log.error("check", `Could not start check: ${error.message}`, error));
  log.info("check", "Check Now started from the dashboard");
  return { started: true };
}

// ---------------------------------------------------------------- HTTP

function send(res, status, body, type = "application/json") {
  res.writeHead(status, { "Content-Type": type, "Cache-Control": "no-store" });
  res.end(type === "application/json" ? JSON.stringify(body) : body);
}

function redirect(res, location) {
  res.writeHead(302, { Location: location, "Cache-Control": "no-store" });
  res.end();
}

async function handle(req, res) {
  const url = new URL(req.url, origin);
  const route = `${req.method} ${url.pathname}`;

  if (route === "GET /" || route === "GET /index.html") {
    return send(res, 200, fs.readFileSync(path.join(ROOT, "dashboard.html")), "text/html; charset=utf-8");
  }
  if (route === "GET /api/health") return send(res, 200, { ok: true });
  if (route === "GET /api/report") {
    return send(res, 200, {
      latest: readJson(PATHS.latest),
      lastSuccess: readJson(PATHS.lastSuccess),
      running: readLock(),
      scheduleTime: config.scheduleTime,
      logTail: tailLog(12),
    });
  }
  if (route === "POST /api/check") return send(res, 202, startCheck());

  if (route === "GET /api/spotify") return send(res, 200, await spotifyNowPlaying());
  if (req.method === "POST" && url.pathname.startsWith("/api/spotify/")) {
    const control = SPOTIFY_CONTROLS[url.pathname.split("/").pop()];
    if (!control) return send(res, 404, { error: "unknown control" });
    const { status, body } = await spotifyApi(...control);
    if (status >= 400) {
      const reason = body.error === "not_connected" ? "Connect Spotify first."
        : body.error?.reason === "PREMIUM_REQUIRED" ? "Spotify Premium is required to control playback."
        : body.error?.reason === "NO_ACTIVE_DEVICE" ? "Open Spotify on this PC or your phone first, then try again."
          : body.error?.message ?? `Spotify error ${status}`;
      return send(res, status, { error: reason });
    }
    return send(res, 200, { ok: true });
  }
  if (route === "GET /spotify/login") {
    if (!config.spotifyClientId) return send(res, 400, "Add your Spotify Client ID to config.json first (see README.txt).", "text/plain");
    const verifier = crypto.randomBytes(48).toString("base64url");
    const state = crypto.randomBytes(16).toString("hex");
    pendingLogins.set(state, verifier);
    const challenge = crypto.createHash("sha256").update(verifier).digest("base64url");
    const params = new URLSearchParams({
      client_id: config.spotifyClientId, response_type: "code", redirect_uri: spotifyRedirect,
      scope: SPOTIFY_SCOPES, state, code_challenge_method: "S256", code_challenge: challenge,
    });
    return redirect(res, `https://accounts.spotify.com/authorize?${params}`);
  }
  if (route === "GET /spotify/callback") {
    const verifier = pendingLogins.get(url.searchParams.get("state"));
    pendingLogins.delete(url.searchParams.get("state"));
    if (!verifier || !url.searchParams.get("code")) {
      log.error("spotify", `Spotify sign-in failed: ${url.searchParams.get("error") ?? "invalid state"}`);
      return redirect(res, "/?spotify=failed");
    }
    try {
      const body = await spotifyTokenRequest({
        grant_type: "authorization_code", code: url.searchParams.get("code"),
        redirect_uri: spotifyRedirect, code_verifier: verifier,
      });
      saveSpotifyToken(body);
      log.info("spotify", "Spotify connected; sign-in saved on this computer");
      return redirect(res, "/?spotify=connected");
    } catch (error) {
      log.error("spotify", error.message, error);
      return redirect(res, "/?spotify=failed");
    }
  }
  if (route === "POST /api/spotify-disconnect") {
    fs.rmSync(PATHS.spotifyToken, { force: true });
    log.info("spotify", "Spotify disconnected");
    return send(res, 200, { ok: true });
  }
  return send(res, 404, { error: "not found" });
}

const server = http.createServer((req, res) => {
  handle(req, res).catch((error) => {
    log.error("http", `${req.method} ${req.url} failed: ${error.message}`, error);
    if (!res.headersSent) send(res, 500, { error: error.message });
  });
});

server.on("error", (error) => {
  if (error.code === "EADDRINUSE") {
    log.info("start", `Dashboard server is already running at ${origin}`);
    process.exit(0);
  }
  log.error("start", `Dashboard server failed: ${error.message}`, error);
  process.exit(1);
});

process.on("uncaughtException", (error) => log.error("fatal", `Uncaught exception: ${error.message}`, error));
process.on("unhandledRejection", (reason) => log.error("fatal", `Unhandled rejection: ${reason?.message ?? reason}`, reason instanceof Error ? reason : undefined));

server.listen(port, "127.0.0.1", () => log.info("start", `Playmaker Jarvis dashboard running at ${origin}`));
