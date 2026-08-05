import { useEffect, useRef, useState } from "react";

import { ApiError, formatBusinessError } from "../../../shared/api/client";
import { POST_MUTATION_REFRESH_FAILED_MESSAGE, runPostMutationRefresh } from "../../../shared/utils/postMutation";
import { downloadVehicleTemplate, confirmVehicleImport, previewVehicleImport } from "../api";
import type { VehicleImportPreview, VehicleImportResult } from "../types";

const MAX_EXCEL_BYTES = 8 * 1024 * 1024;

type Props = {
  open: boolean;
  onClose: () => void;
  onImported: () => Promise<boolean> | boolean;
};

function solutionFor(error: string) {
  if (error.includes("编号格式")) return "仅使用字母、数字、下划线、点和短横线";
  if (error.includes("编号重复")) return "删除重复行或修改车辆编号";
  if (error.includes("展示名称")) return "填写车辆展示名称后重新上传";
  if (error.includes("首次上牌")) return "按 YYYY-MM 格式填写";
  if (error.includes("数字")) return "填写非负数字，不要包含单位或文字";
  if (error.includes("整数")) return "填写不带小数的非负整数";
  if (error.includes("可更新字段")) return "至少填写一个需要更新的车辆字段";
  return "修正该行内容后重新上传并校验";
}

export function VehicleImportModal({ open, onClose, onImported }: Props) {
  const [step, setStep] = useState<"upload" | "preview" | "result">("upload");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<VehicleImportPreview | null>(null);
  const [result, setResult] = useState<VehicleImportResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resultError, setResultError] = useState<string | null>(null);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!open) return;
    setStep("upload");
    setFile(null);
    setPreview(null);
    setResult(null);
    setError(null);
    setResultError(null);
    setRefreshFailed(false);
  }, [open]);

  if (!open) return null;

  function chooseFile(nextFile: File | null) {
    setError(null);
    setPreview(null);
    if (!nextFile) {
      setFile(null);
      return;
    }
    if (!nextFile.name.toLowerCase().endsWith(".xlsx")) {
      setFile(null);
      setError("请选择系统模板生成的 .xlsx 文件。");
      return;
    }
    if (nextFile.size > MAX_EXCEL_BYTES) {
      setFile(null);
      setError("文件不能超过 8MB，请压缩或拆分后重新上传。");
      return;
    }
    setFile(nextFile);
    void handlePreview(nextFile);
  }

  async function downloadTemplate() {
    setBusy(true);
    setError(null);
    try {
      const blob = await downloadVehicleTemplate();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "chejin_vehicle_import_v1.xlsx";
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(formatBusinessError(err, "模板下载失败，请重试。"));
    } finally {
      setBusy(false);
    }
  }

  async function handlePreview(targetFile = file) {
    if (!targetFile) return;
    setBusy(true);
    setError(null);
    try {
      const data = await previewVehicleImport(targetFile);
      setPreview(data);
      setStep("preview");
    } catch (err) {
      setError(formatBusinessError(err, "文件无法预览，请重新下载模板并填写。"));
    } finally {
      setBusy(false);
    }
  }

  async function handleConfirm() {
    if (!preview?.can_confirm) return;
    setBusy(true);
    setResultError(null);
    setRefreshFailed(false);
    let data: VehicleImportResult;
    try {
      data = await confirmVehicleImport(preview.preview_id);
    } catch (err) {
      const errorNumber = err instanceof ApiError ? (err.traceId || err.code) : "未知";
      setResultError(`导入失败，本次未写入任何数据，请稍后重试。错误编号：${errorNumber}`);
      setStep("result");
      setBusy(false);
      return;
    }

    setResult(data);
    setStep("result");
    const refreshed = await runPostMutationRefresh(onImported);
    setRefreshFailed(!refreshed);
    setBusy(false);
  }

  const errors = preview?.rows.flatMap((row) => row.errors.map((message) => ({ row: row.row_number, code: row.vehicle_code, message }))) || [];

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal vehicle-import-modal" role="dialog" aria-modal="true" aria-labelledby="vehicle-import-title">
        <header><div><h2 id="vehicle-import-title">Excel 导入车辆</h2><p>仅接受系统最新模板</p></div></header>
        <ol className="import-stepper" aria-label="导入步骤">
          <li className={step === "upload" ? "is-active" : "is-complete"}><span>1</span>上传文件</li>
          <li className={step === "preview" ? "is-active" : step === "result" ? "is-complete" : ""}><span>2</span>预览校验</li>
          <li className={step === "result" ? "is-active" : ""}><span>3</span>确认结果</li>
        </ol>

        <div className="vehicle-import-body">
          {step === "upload" ? (
            <section className="import-stage is-active">
              <input ref={inputRef} className="sr-only" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={(event) => chooseFile(event.target.files?.[0] || null)} />
              <div className="import-upload-zone">
                <strong>{busy ? "正在解析车辆 Excel" : file ? file.name : "上传车辆 Excel"}</strong>
                <p>{file ? `${(file.size / 1024).toFixed(1)} KB` : "仅支持 .xlsx，最大 8MB，单次最多 2000 行"}</p>
                <button type="button" className="primary-button" disabled={busy} onClick={() => inputRef.current?.click()}>{busy ? "解析中..." : "选择文件"}</button>
              </div>
              <button type="button" className="text-button" disabled={busy} onClick={() => void downloadTemplate()}>下载最新模板</button>
              <p className="import-footnote">上传和解析阶段不会写入正式车辆数据，Excel 不导入车辆图片。</p>
              {error ? <div className="inline-alert error" role="alert">{error}</div> : null}
            </section>
          ) : step === "preview" && preview ? (
            <>
              <section className="import-summary-grid" aria-label="导入预览统计">
                <article><span>车辆总数</span><strong>{preview.total_rows}</strong></article>
                <article><span>新增</span><strong>{preview.create_count}</strong></article>
                <article><span>更新</span><strong>{preview.update_count}</strong></article>
                <article className={preview.error_count ? "has-error" : ""}><span>错误</span><strong>{preview.error_count}</strong></article>
              </section>
              {errors.length ? (
                <div className="import-error-list" role="alert">
                  <strong>请修正以下问题后重新上传</strong>
                  <ul>{errors.map((item, index) => <li key={`${item.row}-${index}`}><span>第 {item.row} 行{item.code ? ` · 车辆 ${item.code}` : ""}</span><b>{item.message}</b><em>{solutionFor(item.message)}</em></li>)}</ul>
                </div>
              ) : (
                <div className="import-ready"><strong>预览校验通过</strong><span>确认后将整批写入；新增车辆默认已下架，更新车辆保持原上下架状态。</span></div>
              )}
              <div className="import-preview-panel">
                <table>
                  <thead><tr><th>位置</th><th>车辆编号</th><th>处理方式</th><th>展示名称</th><th>校验结果</th></tr></thead>
                  <tbody>{preview.rows.map((row) => <tr key={`${row.row_number}-${row.vehicle_code}`}><td>第 {row.row_number} 行</td><td>{row.vehicle_code}</td><td>{row.action === "create" ? "新增" : "更新"}</td><td>{row.data.display_name || "-"}</td><td>{row.errors.length ? `${row.errors.length} 个问题` : "通过"}</td></tr>)}</tbody>
                </table>
              </div>
            </>
          ) : (
            <div className={`import-result ${resultError ? "import-result-error" : ""}`}>
              {resultError ? <><strong>整批导入失败</strong><p>{resultError}</p><span>请返回重新预览，或稍后重试。系统没有写入本批任何车辆数据。</span></> : <><strong>车辆导入完成</strong><p>新增 {result?.create_count || 0} 辆，更新 {result?.update_count || 0} 辆。</p><span>{refreshFailed ? POST_MUTATION_REFRESH_FAILED_MESSAGE : "车辆列表已刷新。"}</span></>}
            </div>
          )}
        </div>

        <footer>
          {step === "upload" ? <button type="button" disabled={busy} onClick={onClose}>取消</button> : null}
          {step === "preview" ? <><button type="button" disabled={busy} onClick={() => { setStep("upload"); setPreview(null); }}>重新选择文件</button><button type="button" className="primary-button" disabled={!preview?.can_confirm || busy} onClick={() => void handleConfirm()}>{busy ? "导入中..." : "确认导入"}</button></> : null}
          {step === "result" ? <><button type="button" onClick={() => { setStep("upload"); setPreview(null); setResult(null); setResultError(null); setRefreshFailed(false); }}>再导入一批</button><button type="button" className="primary-button" onClick={onClose}>返回车辆列表</button></> : null}
        </footer>
      </section>
    </div>
  );
}
