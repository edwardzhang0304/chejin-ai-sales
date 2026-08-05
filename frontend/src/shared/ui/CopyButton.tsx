import { useState } from "react";

import { CopyIcon } from "./Icons";

type Props = {
  label: string;
  value: string | null | undefined;
};

export function CopyButton({ label, value }: Props) {
  const [copied, setCopied] = useState(false);
  const text = value?.trim() || "";

  return (
    <button
      className="copy-button"
      type="button"
      disabled={!text}
      aria-label={`复制${label}`}
      title={copied ? "已复制" : `复制${label}`}
      onClick={(event) => {
        event.stopPropagation();
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1200);
        });
      }}
    >
      <CopyIcon />
      <span className="sr-only" aria-live="polite">{copied ? "已复制" : ""}</span>
    </button>
  );
}
