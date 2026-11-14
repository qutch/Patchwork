// Test harness for UI tests that run against the real mock control plane (web/mock/server.mjs) on a free port.
import { spawn, type ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { setApiBase } from "../api";

const HERE = dirname(fileURLToPath(import.meta.url));

export interface Mock {
  base: string;
  stop(): void;
  /** JSON call straight at the mock (not through the UI) */
  call<T = unknown>(method: string, path: string, body?: unknown): Promise<T>;
}

/** Start the mock with no pacing and point the API client at it. */
export async function startMock(): Promise<Mock> {
  const proc: ChildProcess = spawn(process.execPath, [join(HERE, "..", "..", "mock", "server.mjs"), "--port", "0", "--pace", "0"], { stdio: ["ignore", "pipe", "inherit"] });
  const port = await new Promise<number>((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("mock did not start")), 8000);
    proc.stdout!.on("data", (b: Buffer) => {
      const m = String(b).match(/127\.0\.0\.1:(\d+)/);
      if (m) {
        clearTimeout(t);
        resolve(Number(m[1]));
      }
    });
  });
  const base = `http://127.0.0.1:${port}`;
  setApiBase(base);
  installEventSource();
  if (typeof window !== "undefined") window.scrollTo = () => {}; // jsdom has none
  return {
    base,
    stop: () => {
      proc.kill();
      setApiBase("");
    },
    async call<T>(method: string, path: string, body?: unknown) {
      const r = await fetch(base + path, { method, headers: { "content-type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
      const text = await r.text();
      if (!r.ok) throw new Error(`${method} ${path}: ${r.status} ${text}`);
      return (text ? JSON.parse(text) : null) as T;
    },
  };
}

/** jsdom has no EventSource: a small fetch-based one (named events, id, reconnect left to the caller). */
export function installEventSource() {
  if ((globalThis as { EventSource?: unknown }).EventSource) return;
  class FetchEventSource {
    readyState = 0;
    onopen: (() => void) | null = null;
    onerror: (() => void) | null = null;
    private listeners = new Map<string, Set<(e: MessageEvent) => void>>();
    private ctl = new AbortController();
    constructor(url: string) {
      void this.run(url);
    }
    addEventListener(name: string, fn: (e: MessageEvent) => void) {
      if (!this.listeners.has(name)) this.listeners.set(name, new Set());
      this.listeners.get(name)!.add(fn);
    }
    close() {
      this.readyState = 2;
      this.ctl.abort();
    }
    private async run(url: string) {
      try {
        const res = await fetch(url, { signal: this.ctl.signal, headers: { accept: "text/event-stream" } });
        this.readyState = 1;
        this.onopen?.();
        const reader = res.body!.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i: number;
          while ((i = buf.indexOf("\n\n")) >= 0) {
            const block = buf.slice(0, i);
            buf = buf.slice(i + 2);
            let id = "", event = "message", data = "";
            for (const line of block.split("\n")) {
              if (line.startsWith("id:")) id = line.slice(3).trim();
              else if (line.startsWith("event:")) event = line.slice(6).trim();
              else if (line.startsWith("data:")) data += line.slice(5).trim();
            }
            if (!data) continue;
            for (const fn of this.listeners.get(event) ?? []) fn({ type: event, data, lastEventId: id } as MessageEvent);
          }
        }
        this.readyState = 2;
        this.onerror?.();
      } catch {
        if (this.readyState !== 2) {
          this.readyState = 2;
          this.onerror?.();
        }
      }
    }
  }
  (globalThis as { EventSource?: unknown }).EventSource = FetchEventSource;
}

/** The text a person would read, for the no-job-words check. */
export const visibleText = (el: Element) => (el as HTMLElement).innerText ?? el.textContent ?? "";

/** The job words from Task 6, applied to the new flow's copy. */
export const JOB_WORDS = /\b(job|posting|employer|company|hiring|applicant|resume)\b/i;
