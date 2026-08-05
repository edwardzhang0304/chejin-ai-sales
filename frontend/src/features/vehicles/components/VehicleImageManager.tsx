import { useEffect, useRef, useState } from "react";
import type { ChangeEvent, DragEvent, KeyboardEvent } from "react";

import { formatBusinessError } from "../../../shared/api/client";
import { ConfirmModal } from "../../../shared/ui/ConfirmModal";
import { UploadIcon } from "../../../shared/ui/Icons";
import { postMutationMessage, runPostMutationRefresh } from "../../../shared/utils/postMutation";
import { deleteVehicleImage, reorderVehicleImages, uploadVehicleImages } from "../api";
import type { VehicleImage, VehicleListingStatus } from "../types";
import { AuthenticatedVehicleImage } from "./AuthenticatedVehicleImage";

const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);

const uploadErrorText: Record<string, string> = {
  VEHICLE_IMAGE_EMPTY: "图片文件为空",
  VEHICLE_IMAGE_TOO_LARGE: "图片超过 10MB",
  VEHICLE_IMAGE_TYPE_INVALID: "仅支持 JPEG、PNG、WebP",
  VEHICLE_IMAGE_DUPLICATED: "同一车辆中已存在相同图片",
};

type Failure = { filename: string; reason: string };

type Props = {
  vehicleCode: string;
  listingStatus: VehicleListingStatus;
  images: VehicleImage[];
  disabled: boolean;
  onChanged: () => Promise<boolean> | boolean;
  onNotify: (message: string, tone?: "success" | "error") => void;
};

function moveItem(items: VehicleImage[], fromIndex: number, toIndex: number) {
  const next = [...items];
  const [moved] = next.splice(fromIndex, 1);
  next.splice(toIndex, 0, moved);
  return next;
}

export function VehicleImageManager({ vehicleCode, listingStatus, images, disabled, onChanged, onNotify }: Props) {
  const [orderedImages, setOrderedImages] = useState(images);
  const [uploading, setUploading] = useState(false);
  const [sorting, setSorting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [failures, setFailures] = useState<Failure[]>([]);
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(images[0]?.id || null);
  const [deleteTarget, setDeleteTarget] = useState<VehicleImage | null>(null);
  const [preview, setPreview] = useState<VehicleImage | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    setOrderedImages(images);
    setSelectedId((current) => images.some((image) => image.id === current) ? current : (images[0]?.id || null));
  }, [images]);

  async function saveOrder(next: VehicleImage[], previous: VehicleImage[]) {
    setOrderedImages(next);
    setSorting(true);
    try {
      await reorderVehicleImages(vehicleCode, next.map((image) => image.id));
    } catch (error) {
      setOrderedImages(previous);
      onNotify(formatBusinessError(error, "图片排序保存失败，请重试。"), "error");
      setSorting(false);
      return;
    }
    const refreshed = await runPostMutationRefresh(onChanged);
    onNotify(postMutationMessage("车辆图片顺序已保存。", refreshed), "success");
    setSorting(false);
  }

  async function handleFiles(event: ChangeEvent<HTMLInputElement>) {
    const selected = Array.from(event.target.files || []);
    event.target.value = "";
    if (!selected.length) return;
    const invalid: Failure[] = [];
    const valid = selected.filter((file) => {
      if (!IMAGE_TYPES.has(file.type)) {
        invalid.push({ filename: file.name, reason: "仅支持 JPEG、PNG、WebP" });
        return false;
      }
      if (file.size > MAX_IMAGE_BYTES) {
        invalid.push({ filename: file.name, reason: "图片超过 10MB" });
        return false;
      }
      return true;
    });
    setFailures(invalid);
    if (!valid.length) return;
    setUploading(true);
    let result: Awaited<ReturnType<typeof uploadVehicleImages>>;
    try {
      result = await uploadVehicleImages(vehicleCode, valid);
    } catch (error) {
      setFailures([...invalid, ...valid.map((file) => ({ filename: file.name, reason: "上传请求失败，请重试" }))]);
      onNotify(formatBusinessError(error, "图片上传失败，请重试。"), "error");
      setUploading(false);
      return;
    }

    const serverFailures = result.items
      .filter((item) => !item.ok)
      .map((item) => ({ filename: item.filename, reason: uploadErrorText[item.error_code || ""] || "上传失败，请重试" }));
    setFailures([...invalid, ...serverFailures]);
    if (result.succeeded) {
      const refreshed = await runPostMutationRefresh(onChanged);
      const uploadMessage = `已上传 ${result.succeeded} 张图片${result.failed ? `，${result.failed} 张失败` : ""}`;
      onNotify(postMutationMessage(`${uploadMessage}。`, refreshed), result.failed ? "error" : "success");
    } else {
      onNotify("所选图片均未上传成功。", "error");
    }
    setUploading(false);
  }

  function handleDrop(event: DragEvent<HTMLLIElement>, targetId: string) {
    event.preventDefault();
    if (!draggedId || draggedId === targetId || sorting) return;
    const fromIndex = orderedImages.findIndex((item) => item.id === draggedId);
    const toIndex = orderedImages.findIndex((item) => item.id === targetId);
    if (fromIndex < 0 || toIndex < 0) return;
    const previous = orderedImages;
    setDraggedId(null);
    void saveOrder(moveItem(previous, fromIndex, toIndex), previous);
  }

  function handleImageKeyDown(event: KeyboardEvent<HTMLLIElement>, index: number) {
    if (!event.altKey || !["ArrowLeft", "ArrowRight"].includes(event.key) || sorting) return;
    event.preventDefault();
    const nextIndex = event.key === "ArrowLeft" ? index - 1 : index + 1;
    if (nextIndex < 0 || nextIndex >= orderedImages.length) return;
    const previous = orderedImages;
    void saveOrder(moveItem(previous, index, nextIndex), previous);
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await deleteVehicleImage(vehicleCode, deleteTarget.id);
    } catch (error) {
      onNotify(formatBusinessError(error, "删除图片失败，请重试。"), "error");
      setDeleting(false);
      return;
    }
    const deletedId = deleteTarget.id;
    setDeleteTarget(null);
    setOrderedImages((current) => current.filter((image) => image.id !== deletedId));
    const refreshed = await runPostMutationRefresh(onChanged);
    onNotify(postMutationMessage("车辆图片已删除。", refreshed), "success");
    setDeleting(false);
  }

  const lastListedImage = listingStatus === "listed" && orderedImages.length <= 1;
  const selectedImage = orderedImages.find((image) => image.id === selectedId) || orderedImages[0] || null;

  return (
    <div className="vehicle-image-manager vehicle-gallery">
      {orderedImages.length === 0 ? (
        <div className="vehicle-images-empty">
          <strong>暂无车辆图片</strong>
          <span>{disabled ? "当前车辆尚未上传图片。" : "上传 JPEG、PNG 或 WebP，单张不超过 10MB。"}</span>
        </div>
      ) : (
        <>
          <button className="vehicle-main-image-wrap" type="button" onClick={() => selectedImage && setPreview(selectedImage)} aria-label="查看车辆主图大图">
            {selectedImage ? <AuthenticatedVehicleImage imageId={selectedImage.id} alt="车辆主图" className="vehicle-main-image" /> : null}
            {selectedImage?.id === orderedImages[0]?.id ? <span className="vehicle-main-badge">主图</span> : null}
          </button>
          <ol className="vehicle-image-strip" aria-label="车辆图片排序">
          {orderedImages.map((image, index) => (
            <li
              key={image.id}
              draggable={!disabled && !sorting}
              tabIndex={0}
              className={draggedId === image.id ? "is-dragging" : ""}
              onDragStart={() => setDraggedId(image.id)}
              onDragEnd={() => setDraggedId(null)}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => handleDrop(event, image.id)}
              onKeyDown={(event) => handleImageKeyDown(event, index)}
            >
              <button className={selectedImage?.id === image.id ? "is-current" : undefined} type="button" onClick={() => setSelectedId(image.id)} aria-label={`选择车辆图片 ${index + 1}`}>
                <AuthenticatedVehicleImage imageId={image.id} alt={image.original_filename} />
              </button>
            </li>
          ))}
          </ol>
        </>
      )}

      {!disabled ? (
        <div className="vehicle-image-actions">
          <input ref={fileInputRef} className="sr-only" type="file" accept="image/jpeg,image/png,image/webp" multiple onChange={(event) => void handleFiles(event)} />
          <div>
            <button type="button" className="secondary-button icon-text-button" disabled={uploading || sorting} onClick={() => fileInputRef.current?.click()}><UploadIcon />{uploading ? "上传中..." : "上传图片"}</button>
            <button type="button" className="secondary-button" disabled={!selectedImage || lastListedImage || deleting || sorting} title={lastListedImage ? "已上架车辆不能删除最后一张图片" : "删除当前图片"} onClick={() => selectedImage && setDeleteTarget(selectedImage)}>删除当前图片</button>
          </div>
          <span>可拖动缩略图排序；JPEG / PNG / WebP，单张最大 10MB</span>
        </div>
      ) : null}

      {sorting ? <p className="vehicle-image-status" role="status">正在保存图片顺序...</p> : null}
      {failures.length ? (
        <div className="vehicle-upload-errors" role="alert">
          <strong>以下图片未上传成功</strong>
          <ul>{failures.map((failure, index) => <li key={`${failure.filename}-${index}`}><span>{failure.filename}</span><em>{failure.reason}</em></li>)}</ul>
        </div>
      ) : null}

      {preview ? (
        <div className="modal-backdrop vehicle-preview-backdrop" role="presentation">
          <section className="modal vehicle-preview-modal" role="dialog" aria-modal="true" aria-label="车辆图片大图预览">
            <div className="vehicle-preview-stage"><AuthenticatedVehicleImage imageId={preview.id} alt={preview.original_filename} /></div>
            <footer><span title={preview.original_filename}>{preview.original_filename}</span><button type="button" className="primary-button" onClick={() => setPreview(null)}>关闭</button></footer>
          </section>
        </div>
      ) : null}

      <ConfirmModal
        open={Boolean(deleteTarget)}
        title="删除这张车辆图片？"
        description="删除后图片无法在车辆资料中继续使用；如果删除的是主图，下一张图片会自动成为主图。"
        confirmLabel="确认删除"
        dangerous
        busy={deleting}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={() => void confirmDelete()}
      />
    </div>
  );
}
