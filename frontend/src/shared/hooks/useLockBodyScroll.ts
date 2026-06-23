import { useEffect } from "react";

let lockCount = 0;
let scrollY = 0;
let previousBodyOverflow = "";
let previousBodyPosition = "";
let previousBodyTop = "";
let previousBodyWidth = "";
let previousHtmlOverflow = "";

export function useLockBodyScroll(active = true) {
  useEffect(() => {
    if (!active || typeof document === "undefined" || typeof window === "undefined") return;

    if (lockCount === 0) {
      scrollY = window.scrollY;
      previousBodyOverflow = document.body.style.overflow;
      previousBodyPosition = document.body.style.position;
      previousBodyTop = document.body.style.top;
      previousBodyWidth = document.body.style.width;
      previousHtmlOverflow = document.documentElement.style.overflow;

      document.documentElement.style.overflow = "hidden";
      document.body.style.overflow = "hidden";
      document.body.style.position = "fixed";
      document.body.style.top = `-${scrollY}px`;
      document.body.style.width = "100%";
    }

    lockCount += 1;

    return () => {
      lockCount = Math.max(0, lockCount - 1);
      if (lockCount === 0) {
        document.documentElement.style.overflow = previousHtmlOverflow;
        document.body.style.overflow = previousBodyOverflow;
        document.body.style.position = previousBodyPosition;
        document.body.style.top = previousBodyTop;
        document.body.style.width = previousBodyWidth;
        window.scrollTo(0, scrollY);

        scrollY = 0;
        previousBodyOverflow = "";
        previousBodyPosition = "";
        previousBodyTop = "";
        previousBodyWidth = "";
        previousHtmlOverflow = "";
      }
    };
  }, [active]);
}
