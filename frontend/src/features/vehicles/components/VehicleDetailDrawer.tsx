import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";

import { formatBusinessError } from "../../../shared/api/client";
import { ConfirmModal } from "../../../shared/ui/ConfirmModal";
import { CopyButton } from "../../../shared/ui/CopyButton";
import { CloseIcon } from "../../../shared/ui/Icons";
import { postMutationMessage, runPostMutationRefresh } from "../../../shared/utils/postMutation";
import { setVehicleListed, updateVehicle } from "../api";
import type { VehicleEditableFields, VehicleItem } from "../types";
import { VehicleImageManager } from "./VehicleImageManager";

type VehicleFormState = Record<keyof VehicleEditableFields, string>;

type Props = {
  vehicle: VehicleItem | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onClose: () => void;
  onDirtyChange: (dirty: boolean) => void;
  onVehicleChanged: (vehicle?: VehicleItem) => Promise<boolean> | boolean;
  onNotify: (message: string, tone?: "success" | "error") => void;
};

const textFields: Array<keyof VehicleEditableFields> = [
  "display_name", "brand", "series", "model", "first_registration", "exterior_color", "interior_color", "location", "customer_description", "vin", "plate_number", "internal_notes",
];

function toForm(vehicle: VehicleItem): VehicleFormState {
  return {
    display_name: vehicle.display_name || "",
    brand: vehicle.brand || "",
    series: vehicle.series || "",
    model: vehicle.model || "",
    public_price: vehicle.public_price === null ? "" : String(vehicle.public_price),
    first_registration: vehicle.first_registration || "",
    mileage_km: vehicle.mileage_km === null ? "" : String(vehicle.mileage_km),
    exterior_color: vehicle.exterior_color || "",
    interior_color: vehicle.interior_color || "",
    location: vehicle.location || "",
    customer_description: vehicle.customer_description || "",
    vin: vehicle.vin || "",
    plate_number: vehicle.plate_number || "",
    purchase_price: vehicle.purchase_price === null ? "" : String(vehicle.purchase_price),
    internal_notes: vehicle.internal_notes || "",
  };
}

function buildPatch(vehicle: VehicleItem, form: VehicleFormState): Partial<VehicleEditableFields> {
  const initial = toForm(vehicle);
  const patch: Record<string, string | number | null> = {};
  textFields.forEach((field) => {
    if (form[field] !== initial[field]) patch[field] = form[field].trim() || null;
  });
  if (form.public_price !== initial.public_price) patch.public_price = form.public_price === "" ? null : Number(form.public_price);
  if (form.purchase_price !== initial.purchase_price) patch.purchase_price = form.purchase_price === "" ? null : Number(form.purchase_price);
  if (form.mileage_km !== initial.mileage_km) patch.mileage_km = form.mileage_km === "" ? null : Number(form.mileage_km);
  return patch as Partial<VehicleEditableFields>;
}

function formatDate(value: string | null | undefined) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const parts = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(date);
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value || "";
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}`;
}

function formatMoney(value: number | string | null) {
  if (value === null || value === "") return "-";
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} 万元` : String(value);
}

function formatMileage(value: number | null) {
  if (value === null) return "-";
  return value >= 10_000 ? `${Number((value / 10_000).toFixed(1))} 万公里` : `${value.toLocaleString("zh-CN")} 公里`;
}

function maskVin(value: string | null) {
  if (!value) return "-";
  return value.length <= 8 ? "******" : `${value.slice(0, Math.max(6, value.length - 6))}******`;
}

function maskPlate(value: string | null) {
  if (!value) return "-";
  if (value.length <= 3) return "***";
  const prefixLength = /[·•-]/.test(value[2] || "") ? 3 : 2;
  return `${value.slice(0, prefixLength)}***${value.slice(-2)}`;
}

function Value({ children }: { children: string | number | null | undefined }) {
  const value = children === null || children === undefined || children === "" ? "-" : String(children);
  return <span title={value}>{value}</span>;
}

function ReadRow({ label, value, copy, copyValue }: { label: string; value: string | number | null | undefined; copy?: boolean; copyValue?: string }) {
  return <div><dt>{label}</dt><dd className={copy ? "copy-value" : undefined}><Value>{value}</Value>{copy ? <CopyButton label={label} value={copyValue ?? (value === null || value === undefined ? "" : String(value))} /> : null}</dd></div>;
}

function EditRow({ label, required, children }: { label: string; required?: boolean; children: React.ReactNode }) {
  return <label><span>{label}{required ? <b> *</b> : null}</span>{children}</label>;
}

export function VehicleDetailDrawer({ vehicle, loading, error, onRetry, onClose, onDirtyChange, onVehicleChanged, onNotify }: Props) {
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<VehicleFormState | null>(vehicle ? toForm(vehicle) : null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [discardOpen, setDiscardOpen] = useState(false);
  const [listingConfirm, setListingConfirm] = useState<"list" | "unlist" | null>(null);
  const [listingBusy, setListingBusy] = useState(false);
  const [listingMissing, setListingMissing] = useState<string[]>([]);

  useEffect(() => {
    setEditing(false);
    setForm(vehicle ? toForm(vehicle) : null);
    setSaveError(null);
    setListingMissing([]);
  }, [vehicle?.vehicle_code]);

  const dirty = useMemo(() => Boolean(vehicle && form && Object.keys(buildPatch(vehicle, form)).length), [vehicle, form]);
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange(false), [onDirtyChange]);

  function requestClose() {
    onClose();
  }

  function requestCancelEdit() {
    if (dirty) {
      setDiscardOpen(true);
      return;
    }
    setEditing(false);
  }

  function confirmDiscard() {
    setDiscardOpen(false);
    onDirtyChange(false);
    if (vehicle) setForm(toForm(vehicle));
    setEditing(false);
    setSaveError(null);
  }

  async function handleSave(event: FormEvent) {
    event.preventDefault();
    if (!vehicle || !form) return;
    if (!form.display_name.trim()) {
      setSaveError("车辆展示名称不能为空。");
      return;
    }
    const patch = buildPatch(vehicle, form);
    if (!Object.keys(patch).length) {
      setEditing(false);
      return;
    }
    setSaving(true);
    setSaveError(null);
    let updated: VehicleItem;
    try {
      updated = await updateVehicle(vehicle.vehicle_code, patch);
    } catch (err) {
      setSaveError(formatBusinessError(err, "车辆资料保存失败，请重试。"));
      setSaving(false);
      return;
    }

    setForm(toForm(updated));
    setEditing(false);
    onDirtyChange(false);
    const refreshed = await runPostMutationRefresh(() => onVehicleChanged(updated));
    onNotify(postMutationMessage("车辆资料已保存。", refreshed), "success");
    setSaving(false);
  }

  function requestListingChange() {
    if (!vehicle) return;
    if (vehicle.listing_status === "listed") {
      setListingConfirm("unlist");
      return;
    }
    const missing: string[] = [];
    if (!vehicle.display_name.trim()) missing.push("车辆展示名称");
    if (!vehicle.public_price || Number(vehicle.public_price) <= 0) missing.push("大于 0 的公开售价");
    if (!vehicle.images.length) missing.push("至少一张有效车辆图片");
    setListingMissing(missing);
    if (!missing.length) setListingConfirm("list");
  }

  async function confirmListingChange() {
    if (!vehicle || !listingConfirm) return;
    const listed = listingConfirm === "list";
    setListingBusy(true);
    let updated: VehicleItem;
    try {
      updated = await setVehicleListed(vehicle.vehicle_code, listed);
    } catch (err) {
      setListingConfirm(null);
      onNotify(formatBusinessError(err, listed ? "车辆上架失败，请重试。" : "车辆下架失败，请重试。"), "error");
      setListingBusy(false);
      return;
    }

    setListingConfirm(null);
    setListingMissing([]);
    const refreshed = await runPostMutationRefresh(() => onVehicleChanged(updated));
    const successMessage = listed ? "车辆已上架。" : "车辆已下架。";
    onNotify(postMutationMessage(successMessage, refreshed), "success");
    setListingBusy(false);
  }

  return (
    <aside className={`panel management-drawer vehicle-detail-drawer ${editing ? "is-editing" : ""}`} aria-label="车辆详情">
      <div className="drawer-head vehicle-drawer-head">
        <div>
          <p>{editing ? "车辆详情 · 编辑中" : "车辆详情"}</p>
          <h2 title={vehicle?.display_name || "车辆详情"}>{vehicle?.display_name || "车辆详情"}</h2>
        </div>
        <button className="icon-button drawer-close-button" type="button" onClick={requestClose} aria-label="关闭车辆详情"><CloseIcon /></button>
      </div>

      {loading ? <div className="state-box">正在加载车辆详情...</div> : error ? (
        <div className="state-box error"><span>{error}</span><button type="button" onClick={onRetry}>重试</button></div>
      ) : !vehicle || !form ? <div className="state-box">车辆详情不存在或已不可访问。</div> : (
        <form className="vehicle-drawer-form" onSubmit={(event) => void handleSave(event)}>
          <div className="vehicle-drawer-scroll">
            <section className="drawer-section vehicle-image-section">
              <VehicleImageManager
                vehicleCode={vehicle.vehicle_code}
                listingStatus={vehicle.listing_status}
                images={vehicle.images}
                disabled={!editing}
                onChanged={() => onVehicleChanged()}
                onNotify={onNotify}
              />
            </section>

            <section className="drawer-section">
              <h3>客户可用资料</h3>
              {editing ? (
                <div className="vehicle-form-grid">
                  <EditRow label="车辆展示名称" required><input maxLength={200} value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} /></EditRow>
                  <EditRow label="品牌"><input maxLength={100} value={form.brand} onChange={(event) => setForm({ ...form, brand: event.target.value })} /></EditRow>
                  <EditRow label="车系"><input maxLength={100} value={form.series} onChange={(event) => setForm({ ...form, series: event.target.value })} /></EditRow>
                  <EditRow label="车型"><input maxLength={200} value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} /></EditRow>
                  <EditRow label="公开售价"><input type="number" min="0" step="0.01" value={form.public_price} onChange={(event) => setForm({ ...form, public_price: event.target.value })} /></EditRow>
                  <EditRow label="首次上牌"><input type="month" min="1900-01" max="2099-12" value={form.first_registration} onChange={(event) => setForm({ ...form, first_registration: event.target.value })} /></EditRow>
                  <EditRow label="表显里程"><input type="number" min="0" max="10000000" step="1" value={form.mileage_km} onChange={(event) => setForm({ ...form, mileage_km: event.target.value })} /></EditRow>
                  <EditRow label="车身颜色"><input maxLength={64} value={form.exterior_color} onChange={(event) => setForm({ ...form, exterior_color: event.target.value })} /></EditRow>
                  <EditRow label="内饰颜色"><input maxLength={64} value={form.interior_color} onChange={(event) => setForm({ ...form, interior_color: event.target.value })} /></EditRow>
                  <EditRow label="车辆所在地"><input maxLength={128} value={form.location} onChange={(event) => setForm({ ...form, location: event.target.value })} /></EditRow>
                  <EditRow label="车辆描述"><textarea rows={4} maxLength={5000} value={form.customer_description} onChange={(event) => setForm({ ...form, customer_description: event.target.value })} /></EditRow>
                </div>
              ) : (
                <dl className="drawer-dl">
                  <ReadRow label="展示名称" value={vehicle.display_name} />
                  <ReadRow label="品牌" value={vehicle.brand} />
                  <ReadRow label="车系" value={vehicle.series} />
                  <ReadRow label="车型" value={vehicle.model} />
                  <ReadRow label="公开售价" value={formatMoney(vehicle.public_price)} />
                  <ReadRow label="首次上牌" value={vehicle.first_registration} />
                  <ReadRow label="表显里程" value={formatMileage(vehicle.mileage_km)} />
                  <ReadRow label="车身颜色" value={vehicle.exterior_color} />
                  <ReadRow label="内饰颜色" value={vehicle.interior_color} />
                  <ReadRow label="车辆所在地" value={vehicle.location} />
                  <ReadRow label="车辆描述" value={vehicle.customer_description} />
                </dl>
              )}
            </section>

            <section className="drawer-section internal-section">
              <div className="drawer-section-title"><h3>内部资料</h3><span>仅内部可见</span></div>
              {editing ? (
                <div className="vehicle-form-grid">
                  <EditRow label="VIN 码"><input maxLength={64} value={form.vin} onChange={(event) => setForm({ ...form, vin: event.target.value })} /></EditRow>
                  <EditRow label="车牌号"><input maxLength={32} value={form.plate_number} onChange={(event) => setForm({ ...form, plate_number: event.target.value })} /></EditRow>
                  <EditRow label="采购价格"><input type="number" min="0" step="0.01" value={form.purchase_price} onChange={(event) => setForm({ ...form, purchase_price: event.target.value })} /></EditRow>
                  <EditRow label="内部备注"><textarea rows={3} maxLength={5000} value={form.internal_notes} onChange={(event) => setForm({ ...form, internal_notes: event.target.value })} /></EditRow>
                </div>
              ) : (
                <dl className="drawer-dl">
                  <ReadRow label="VIN 码" value={maskVin(vehicle.vin)} copy copyValue={vehicle.vin || ""} />
                  <ReadRow label="车牌号" value={maskPlate(vehicle.plate_number)} copy copyValue={vehicle.plate_number || ""} />
                  <ReadRow label="采购价格" value={formatMoney(vehicle.purchase_price)} />
                  <ReadRow label="内部备注" value={vehicle.internal_notes} />
                </dl>
              )}
            </section>

            <section className="drawer-section">
              <h3>状态与系统信息</h3>
              <dl className="drawer-dl">
                <ReadRow label="车辆编号" value={vehicle.vehicle_code} copy />
                <ReadRow label="上下架状态" value={vehicle.listing_status === "listed" ? "已上架" : "已下架"} />
                <ReadRow label="创建时间" value={formatDate(vehicle.created_at)} />
                <ReadRow label="更新时间" value={formatDate(vehicle.updated_at)} />
              </dl>
            </section>
          </div>

          <footer className="vehicle-drawer-actions">
            {saveError ? <div className="inline-alert error" role="alert">{saveError}</div> : null}
            {listingMissing.length ? <div className="listing-missing" role="alert"><strong>暂不能上架，请补齐：</strong><ul>{listingMissing.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}
            <h3>操作</h3>
            {editing ? (
              <div className="drawer-actions"><button type="button" disabled={saving} onClick={requestCancelEdit}>取消</button><button className="primary-button" type="submit" disabled={saving || !dirty}>{saving ? "保存中..." : "保存"}</button></div>
            ) : (
              <div className="drawer-actions"><button type="button" onClick={() => { setForm(toForm(vehicle)); setEditing(true); setSaveError(null); }}>编辑车辆</button><button type="button" className={vehicle.listing_status === "listed" ? "danger-button" : "primary-button"} onClick={requestListingChange}>{vehicle.listing_status === "listed" ? "下架" : "上架"}</button></div>
            )}
          </footer>
        </form>
      )}

      <ConfirmModal open={discardOpen} title="放弃未保存的修改？" description="当前车辆资料有尚未保存的修改，放弃后无法恢复。" confirmLabel="放弃修改" dangerous onCancel={() => setDiscardOpen(false)} onConfirm={confirmDiscard} />
      <ConfirmModal
        open={Boolean(listingConfirm)}
        title={listingConfirm === "list" ? "确认上架车辆" : "确认下架车辆"}
        description={listingConfirm === "list" ? "上架后，车辆的客户可用资料将立即进入客服查询和回复范围。" : "下架后，车辆不再进入新的客服查询、推荐和回复范围，历史已发送消息不受影响。"}
        confirmLabel={listingConfirm === "list" ? "确认上架" : "确认下架"}
        dangerous={listingConfirm === "unlist"}
        busy={listingBusy}
        onCancel={() => setListingConfirm(null)}
        onConfirm={() => void confirmListingChange()}
      />
    </aside>
  );
}
