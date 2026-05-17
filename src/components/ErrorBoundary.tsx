import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props { children: ReactNode; label?: string }
interface State { error: Error | null; info: ErrorInfo | null }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, info: null };

  static getDerivedStateFromError(error: Error): State {
    return { error, info: null };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.setState({ error, info });
    // eslint-disable-next-line no-console
    console.error("[ErrorBoundary]", this.props.label ?? "root", error, info);
  }

  reset = () => this.setState({ error: null, info: null });

  render() {
    if (!this.state.error) return this.props.children;

    return (
      <div style={{
        padding: 20,
        background: "#1a1f26",
        color: "#f85149",
        fontFamily: "ui-monospace, monospace",
        fontSize: 12,
        lineHeight: 1.5,
        minHeight: "100vh",
        overflow: "auto",
      }}>
        <div style={{ fontSize: 16, marginBottom: 12, fontWeight: 700 }}>
          UI crashed{this.props.label ? ` in ${this.props.label}` : ""}
        </div>
        <div style={{ color: "#e6edf3", marginBottom: 8 }}>
          <strong>{this.state.error.name}:</strong> {this.state.error.message}
        </div>
        <pre style={{ background: "#0e1116", padding: 12, borderRadius: 4, overflow: "auto", color: "#7d8590" }}>
          {this.state.error.stack}
        </pre>
        {this.state.info && (
          <pre style={{ background: "#0e1116", padding: 12, borderRadius: 4, overflow: "auto", color: "#7d8590", marginTop: 8 }}>
            {this.state.info.componentStack}
          </pre>
        )}
        <button onClick={this.reset} style={{
          marginTop: 12, padding: "6px 12px", background: "#2a323e",
          color: "#e6edf3", border: "1px solid #444c56", borderRadius: 4, cursor: "pointer",
        }}>
          Reset
        </button>
      </div>
    );
  }
}
