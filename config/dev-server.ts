import { X509Certificate } from "node:crypto";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ServerOptions } from "vite";

/** Must match SourceLocation in manifest.xml. */
export const TASK_PANE_HOST = "localhost";
export const TASK_PANE_PORT = 5173;
export const TASK_PANE_ORIGIN = `https://${TASK_PANE_HOST}:${TASK_PANE_PORT}`;
export const API_PATH_PREFIX = "/api";
export const DEFAULT_API_PROXY_TARGET = "http://127.0.0.1:8000";

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

export interface DevServerEnv {
  FINLENS_API_PROXY_TARGET?: string;
  FINLENS_DEV_CERTS_DIR?: string;
}

export interface DevServerDeps {
  homeDir: string;
  readFile: (path: string) => string;
  certificateValidTo: (pem: string) => Date;
  now: () => Date;
}

const defaultDeps: DevServerDeps = {
  homeDir: homedir(),
  readFile: (path) => readFileSync(path, "utf8"),
  certificateValidTo: (pem) => new Date(new X509Certificate(pem).validTo),
  now: () => new Date(),
};

const INSTALL_HINT = 'Run "npm run dev:certs" (npx office-addin-dev-certs install) and restart "npm run dev".';

/**
 * The browser only ever talks to the task pane origin; Vite forwards /api to the
 * API on the loopback interface, so the API needs no CORS and is never exposed.
 */
export function resolveApiProxyTarget(raw: string | undefined): string {
  const target = new URL(raw ?? DEFAULT_API_PROXY_TARGET);
  if (!["http:", "https:"].includes(target.protocol) || !LOOPBACK_HOSTS.has(target.hostname) || target.pathname !== "/" || target.username || target.password) {
    throw new Error("FINLENS_API_PROXY_TARGET must be a loopback origin such as http://127.0.0.1:8000.");
  }
  return target.origin;
}

export function loadDevCertificates(env: DevServerEnv, deps: DevServerDeps = defaultDeps): { cert: string; key: string; ca: string } {
  const directory = env.FINLENS_DEV_CERTS_DIR ?? join(deps.homeDir, ".office-addin-dev-certs");
  let certificates: { cert: string; key: string; ca: string };
  try {
    certificates = {
      cert: deps.readFile(join(directory, "localhost.crt")),
      key: deps.readFile(join(directory, "localhost.key")),
      ca: deps.readFile(join(directory, "ca.crt")),
    };
  } catch {
    throw new Error(`Trusted localhost certificates were not found in ${directory}. ${INSTALL_HINT}`);
  }
  // Excel refuses an expired certificate without a useful message, so fail fast here instead.
  for (const pem of [certificates.cert, certificates.ca]) {
    if (deps.certificateValidTo(pem) <= deps.now()) {
      throw new Error(`The localhost development certificate in ${directory} has expired. ${INSTALL_HINT}`);
    }
  }
  return certificates;
}

export function createDevServerConfig(env: DevServerEnv, deps: DevServerDeps = defaultDeps): ServerOptions {
  return {
    host: TASK_PANE_HOST,
    port: TASK_PANE_PORT,
    strictPort: true,
    // Same-origin only: no CORS grant for other localhost ports or any other origin.
    cors: false,
    https: loadDevCertificates(env, deps),
    proxy: {
      [API_PATH_PREFIX]: { target: resolveApiProxyTarget(env.FINLENS_API_PROXY_TARGET), changeOrigin: false, xfwd: false },
    },
  };
}
