import { Component, type ErrorInfo, type PropsWithChildren } from "react";

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<PropsWithChildren, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error("ErrorBoundary caught:", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div
          style={{
            minHeight: "100vh",
            display: "grid",
            placeItems: "center",
            padding: "var(--space-5)",
          }}
        >
          <div
            className="glass-card glass-card--strong"
            style={{ width: "min(500px, 90vw)", textAlign: "center" }}
          >
            <h2 style={{ marginTop: 0, color: "var(--danger)" }}>
              Something went wrong
            </h2>
            <p className="muted" style={{ lineHeight: 1.6, wordBreak: "break-word" }}>
              {this.state.error.message}
            </p>
            <button
              className="glass-button glass-button--primary"
              style={{ marginTop: "var(--space-4)" }}
              onClick={() => {
                this.setState({ error: null });
                window.location.reload();
              }}
            >
              Reload
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
