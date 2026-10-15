// @vitest-environment node
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { API_PATH_PREFIX, createDevServerConfig, DEFAULT_API_PROXY_TARGET, resolveApiProxyTarget, TASK_PANE_ORIGIN } from "./dev-server";

const certs: Record<string, string> = { "/certs/localhost.crt": "CERT", "/certs/localhost.key": "KEY", "/certs/ca.crt": "CA" };
const deps = {
  homeDir: "/home/analyst",
  readFile: (path: string) => { if (!(path in certs)) throw new Error("ENOENT"); return certs[path]; },
  certificateValidTo: () => new Date("2026-12-01T00:00:00Z"),
  now: () => new Date("2026-10-14T00:00:00Z"),
};

function sourceFiles(directory: string): string[] {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    return statSync(path).isDirectory() ? sourceFiles(path) : /\.tsx?$/.test(name) && !/\.test\./.test(name) ? [path] : [];
  });
}

describe("dev network configuration", () => {
  it("serves the task pane over trusted HTTPS on localhost only and proxies /api to the loopback API", () => {
    const config = createDevServerConfig({ FINLENS_DEV_CERTS_DIR: "/certs" }, deps);
    expect(config).toMatchObject({ host: "localhost", port: 5173, strictPort: true, cors: false, https: { cert: "CERT", key: "KEY", ca: "CA" } });
    expect(config.proxy).toEqual({ [API_PATH_PREFIX]: { target: DEFAULT_API_PROXY_TARGET, changeOrigin: false, xfwd: false } });
  });

  it("explains how to install certificates instead of silently falling back to HTTP", () => {
    expect(() => createDevServerConfig({}, deps)).toThrow(/office-addin-dev-certs install/);
    expect(() => createDevServerConfig({ FINLENS_DEV_CERTS_DIR: "/certs" }, { ...deps, now: () => new Date("2027-01-01T00:00:00Z") })).toThrow(/has expired.*dev:certs/);
  });

  it("only allows a loopback API proxy target", () => {
    expect(resolveApiProxyTarget("http://localhost:9000")).toBe("http://localhost:9000");
    for (const target of ["http://10.0.0.5:8000", "https://api.example.com", "http://127.0.0.1:8000/other", "http://user:pw@127.0.0.1:8000", "file:///etc/passwd"]) {
      expect(() => resolveApiProxyTarget(target)).toThrow(/loopback/);
    }
  });

  it("keeps manifest, page and client on the single HTTPS task pane origin", () => {
    const manifest = readFileSync("manifest.xml", "utf8");
    const manifestUrls = [...manifest.matchAll(/DefaultValue="(https?:\/\/[^"]+)"/g)].map((match) => match[1]);
    expect(manifestUrls.length).toBeGreaterThan(0);
    manifestUrls.forEach((url) => expect(new URL(url).origin).toBe(TASK_PANE_ORIGIN));

    const scripts = [...readFileSync("index.html", "utf8").matchAll(/src="([^"]+)"/g)].map((match) => match[1]);
    scripts.filter((src) => /^[a-z]+:/i.test(src)).forEach((src) => expect(src).toMatch(/^https:\/\//));

    // The client never addresses the API by absolute URL, so it cannot need CORS or hit mixed content.
    for (const file of sourceFiles("src")) {
      expect(readFileSync(file, "utf8"), file).not.toMatch(/https?:\/\/(localhost|127\.0\.0\.1)/);
    }
  });
});
