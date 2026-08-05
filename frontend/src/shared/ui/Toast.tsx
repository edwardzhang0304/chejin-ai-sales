import { useEffect } from "react";

type Props = {
  message: string | null;
  tone?: "success" | "error";
  onDismiss: () => void;
};

export function Toast({ message, tone = "success", onDismiss }: Props) {
  useEffect(() => {
    if (!message) return;
    const timer = window.setTimeout(onDismiss, 3600);
    return () => window.clearTimeout(timer);
  }, [message, onDismiss]);

  if (!message) return null;
  return (
    <div className={`global-toast ${tone}`} role={tone === "error" ? "alert" : "status"}>
      <span>{message}</span>
      <button type="button" onClick={onDismiss}>知道了</button>
    </div>
  );
}
