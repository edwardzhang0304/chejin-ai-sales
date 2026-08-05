import { useEffect, useState } from "react";

import { readVehicleImage } from "../api";

type Props = {
  imageId: string;
  alt: string;
  className?: string;
  onClick?: () => void;
};

export function AuthenticatedVehicleImage({ imageId, alt, className, onClick }: Props) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | null = null;
    setSrc(null);
    setFailed(false);
    void readVehicleImage(imageId, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      })
      .catch(() => {
        if (!controller.signal.aborted) setFailed(true);
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [imageId]);

  if (failed) return <span className={`vehicle-image-fallback ${className || ""}`} aria-label={`${alt}加载失败`}>图片加载失败</span>;
  if (!src) return <span className={`vehicle-image-skeleton ${className || ""}`} aria-label={`${alt}加载中`} />;
  return <img src={src} alt={alt} className={className} onClick={onClick} />;
}
