import { useEffect, useRef, useState } from "react";

type Props = {
  label: string;
  value: string;
  copyValue?: string | null;
  className?: string;
};

type CopyState = "idle" | "copied" | "failed";

export function ClickToCopyText({ label, value, copyValue, className }: Props) {
  const [copyState, setCopyState] = useState<CopyState>("idle");
  const resetTimer = useRef<number | null>(null);
  const text = (copyValue === undefined ? value : copyValue)?.trim() || "";
  const canCopy = Boolean(text) && text !== "-";

  useEffect(() => () => {
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
  }, []);

  function resetLater() {
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
    resetTimer.current = window.setTimeout(() => setCopyState("idle"), 1200);
  }

  async function copy() {
    if (!canCopy) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
    resetLater();
  }

  const statusText = copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败" : "";
  const classes = ["click-to-copy-text", className].filter(Boolean).join(" ");

  return (
    <button
      className={classes}
      type="button"
      disabled={!canCopy}
      title={canCopy ? `点击复制${label}` : value}
      aria-label={canCopy ? `复制${label}：${value}` : undefined}
      data-copy-state={copyState}
      onClick={(event) => {
        event.stopPropagation();
        void copy();
      }}
      onKeyDown={(event) => event.stopPropagation()}
    >
      <span className="click-to-copy-value">{value}</span>
      {statusText ? <span className="click-to-copy-feedback" aria-hidden="true">{statusText}</span> : null}
      <span className="sr-only" aria-live="polite">{statusText}</span>
    </button>
  );
}
