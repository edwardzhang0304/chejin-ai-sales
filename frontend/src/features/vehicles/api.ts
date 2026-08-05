import { request, requestBlob, requestForm } from "../../shared/api/client";
import type {
  VehicleEditableFields,
  VehicleImage,
  VehicleImportPreview,
  VehicleImportResult,
  VehicleItem,
  VehicleListResult,
  VehicleListingFilter,
  VehicleUploadResult,
} from "./types";

export type VehicleListQuery = {
  keyword?: string;
  listing_status?: VehicleListingFilter;
  page?: number;
  page_size?: number;
};

export function listVehicles(query: VehicleListQuery, signal?: AbortSignal) {
  return request<VehicleListResult>("/vehicles", { query, signal });
}

export function getVehicle(vehicleCode: string, signal?: AbortSignal) {
  return request<VehicleItem>(`/vehicles/${encodeURIComponent(vehicleCode)}`, { signal });
}

export function createVehicle(displayName: string) {
  return request<VehicleItem>("/vehicles", {
    method: "POST",
    body: { display_name: displayName },
  });
}

export function updateVehicle(vehicleCode: string, payload: Partial<VehicleEditableFields>) {
  return request<VehicleItem>(`/vehicles/${encodeURIComponent(vehicleCode)}`, {
    method: "PUT",
    body: payload,
  });
}

export function setVehicleListed(vehicleCode: string, listed: boolean) {
  return request<VehicleItem>(`/vehicles/${encodeURIComponent(vehicleCode)}/${listed ? "list" : "unlist"}`, {
    method: "POST",
  });
}

export function uploadVehicleImages(vehicleCode: string, files: File[]) {
  const formData = new FormData();
  files.forEach((file) => formData.append("files", file));
  return requestForm<VehicleUploadResult>(`/vehicles/${encodeURIComponent(vehicleCode)}/images`, formData);
}

export function reorderVehicleImages(vehicleCode: string, imageIds: string[]) {
  return request<{ items: VehicleImage[] }>(`/vehicles/${encodeURIComponent(vehicleCode)}/images/order`, {
    method: "PUT",
    body: { image_ids: imageIds },
  });
}

export function deleteVehicleImage(vehicleCode: string, imageId: string) {
  return request<{ image_id: string; deleted: boolean }>(
    `/vehicles/${encodeURIComponent(vehicleCode)}/images/${encodeURIComponent(imageId)}`,
    { method: "DELETE" },
  );
}

export function readVehicleImage(imageId: string, signal?: AbortSignal) {
  return requestBlob(`/vehicles/images/${encodeURIComponent(imageId)}`, { signal });
}

export function downloadVehicleTemplate(signal?: AbortSignal) {
  return requestBlob("/vehicles/excel/template", { signal });
}

export function previewVehicleImport(file: File) {
  const formData = new FormData();
  formData.append("file", file);
  return requestForm<VehicleImportPreview>("/vehicles/excel/preview", formData);
}

export function confirmVehicleImport(previewId: string) {
  return request<VehicleImportResult>(`/vehicles/excel/${encodeURIComponent(previewId)}/confirm`, {
    method: "POST",
  });
}
