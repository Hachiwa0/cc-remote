import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props { children: ReactNode }
interface State { error: Error | null }

// Catches render-time errors so a crash shows a message instead of a black
// screen (useful over HTTP LAN where the browser console isn't handy).
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("React error boundary:", error, info);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <pre style={{
          whiteSpace: "pre-wrap", wordBreak: "break-word", padding: 16, margin: 0,
          color: "#f87171", background: "#0e0f13", height: "100%",
          fontFamily: "ui-monospace, Consolas, monospace", fontSize: 13,
        }}>
          {"应用崩溃:\n\n" + (this.state.error.stack || this.state.error.message)}
        </pre>
      );
    }
    return this.props.children;
  }
}
