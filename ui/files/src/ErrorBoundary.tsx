import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** When this value changes - the route - a shown error is cleared.  The pages themselves are not remounted. */
  resetKey?: string;
  /** Shown instead of the default message (for a small place such as the footer). */
  fallback?: ReactNode;
}

interface State {
  failed: boolean;
  message: string;
  resetKey?: string;
}

function describe(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return typeof error === "string" && error ? error : "未預期的錯誤";
}

/**
 * Keeps the rest of the site usable when one part of it cannot be drawn from the data it was given.
 * Without it a single unexpected value blanks the whole site.
 *
 * It catches errors raised while drawing.  An error in a click handler or in a failed request is not one of those:
 * the pages report these themselves ("載入失敗…").
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { failed: false, message: "", resetKey: this.props.resetKey };

  // `failed` is its own flag: whatever was thrown (null, "", 0 ...) counts.
  static getDerivedStateFromError(error: unknown): Partial<State> {
    return { failed: true, message: describe(error) };
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    return props.resetKey !== state.resetKey ? { failed: false, message: "", resetKey: props.resetKey } : null;
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error("this part of the page could not be drawn", error, info.componentStack);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    if (this.props.fallback !== undefined) return this.props.fallback;
    return (
      <div className="card page-error" role="alert">
        <strong>這個頁面無法顯示。</strong>
        <p>
          資料內容和這一版網站預期的格式不一致（{this.state.message}）。導覽列仍可使用；
          讀到同一份資料的其他頁面可能也會這樣。請把這段訊息和頁尾的資料版本回報給平台團隊。
        </p>
      </div>
    );
  }
}
