import { Button, Tooltip } from "@heroui/react";
import { FileArchive } from "lucide-react";
import { useState } from "react";
import { base, blindPending, isFinished, messageOf, type Experiment } from "../api";

export function EvidenceBundle({ run }: { run: Experiment }) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState(false);
  const disabled = blindPending(run.blind_status) ? "盲评揭晓后可导出完整证据包"
    : !isFinished(run.status) ? "评测结束后可导出完整证据包" : "";
  async function download() {
    setBusy(true); setNotice(""); setError(false);
    try {
      const response = await fetch(`${base}/experiments/${encodeURIComponent(run.id)}/evidence-bundle`,
        { credentials: "same-origin" });
      if (!response.ok || !response.headers.get("content-type")?.includes("application/zip")) {
        const body = await response.json().catch(() => null) as { detail?: string; code?: number } | null;
        throw new Error(body?.code === 401 ? "登录已失效，请登录后刷新本页"
          : body?.detail || `证据包导出失败 (${response.status})`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${run.name}-${run.id}-evidence.zip`.replace(/[\\/:*?"<>|]/g, "_");
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setNotice("证据包已下载；完整性与缺失项请查看包内 manifest.json。");
    } catch (cause) { setError(true); setNotice(messageOf(cause)); }
    finally { setBusy(false); }
  }
  return <div className="bundle-export">
    <Tooltip content={disabled || "下载整份报告的输入输出、评分依据和已保存模型调用链；保留业务原值，过滤凭据。"}>
      <span><Button size="sm" color="primary" variant="flat" isLoading={busy}
        isDisabled={!!disabled || busy} startContent={!busy && <FileArchive size={16} />}
        onPress={download}>{busy ? "正在打包证据…" : "导出完整证据包"}</Button></span>
    </Tooltip>
    <small className="muted">供 LLM 分析 · ZIP</small>
    {notice && <span className={`bundle-notice${error ? " error" : ""}`} role={error ? "alert" : "status"}>
      {notice}</span>}
  </div>;
}
