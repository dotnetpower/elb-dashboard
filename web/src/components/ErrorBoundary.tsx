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
    console.error("ErrorBoundary caught:", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      const errorText = `${this.state.error.name}: ${this.state.error.message}\n${this.state.error.stack ?? ""}`;
      return (
        <div
          role="alert"
          style={{
            minHeight: "100vh",
            display: "grid",
            placeItems: "center",
            padding: "var(--space-5)",
          }}
        >
          <div
            className="glass-card glass-card--strong"
            style={{ width: "min(520px, 90vw)", textAlign: "center" }}
          >
            <h2 style={{ marginTop: 0, color: "var(--danger)" }}>Something went wrong</h2>
            <p className="muted" style={{ lineHeight: 1.6, wordBreak: "break-word" }}>
              {this.state.error.message}
            </p>
            <div
              style={{
                marginTop: "var(--space-3)",
                padding: "12px",
                borderRadius: 6,
                background: "var(--bg-tertiary)",
                fontSize: 12,
                color: "var(--text-muted)",
                textAlign: "left",
                lineHeight: 1.5,
              }}
            >
              <strong>If this keeps happening, try:</strong>
              <ol style={{ margin: "8px 0 0", paddingLeft: 20 }}>
                <li>Sign out and sign back in</li>
                <li>Reload the current page</li>
                <li>Contact your administrator with the copied error details</li>
              </ol>
            </div>
            <div
              style={{
                display: "flex",
                gap: 8,
                justifyContent: "center",
                marginTop: "var(--space-4)",
                flexWrap: "wrap",
              }}
            >
              <button
                className="glass-button"
                onClick={() => {
                  navigator.clipboard.writeText(errorText).catch(() => {});
                }}
              >
                Copy error details
              </button>
              <button
                className="glass-button"
                onClick={() => this.setState({ error: null })}
              >
                Try again
              </button>
              <button
                className="glass-button"
                onClick={() => {
                  this.setState({ error: null });
                  window.location.reload();
                }}
              >
                Reload
              </button>
              <button
                className="glass-button glass-button--primary"
                onClick={() => {
                  window.location.assign("/");
                }}
              >
                Dashboard
              </button>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
