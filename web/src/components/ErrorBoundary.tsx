import { Component, type ErrorInfo, type ReactNode } from "react";

const SAVED_KEYS = ["pw-recording", "pw-thread", "pw-run"];

/** A render error shows what broke and a way out, instead of a blank page. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Patchwork console crashed", error, info.componentStack);
  }

  private clearAndReload = () => {
    for (const k of SAVED_KEYS) {
      try {
        localStorage.removeItem(k);
      } catch {
        /* storage blocked: the reload alone may still help */
      }
    }
    location.reload();
  };

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <main className="mx-auto max-w-[640px] p-6 grid gap-3">
        <h1 className="text-xl font-semibold">The console hit an error</h1>
        <p>
          This usually means the browser remembered a recording, agent thread or run from before a demo reset.
          Clearing that saved selection and reloading fixes it.
        </p>
        <pre className="text-sm whitespace-pre-wrap opacity-80">{this.state.error.message}</pre>
        <div>
          <button className="btn" onClick={this.clearAndReload}>Clear saved selection and reload</button>
        </div>
      </main>
    );
  }
}
