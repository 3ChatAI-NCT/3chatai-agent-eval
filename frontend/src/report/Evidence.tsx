import { Button } from "@heroui/react";
import { Copy } from "lucide-react";
import { useState } from "react";
import { messageOf, api, type Evidence } from "../api";
import { Notice } from "../App";

const EVIDENCE_TABS: [keyof Evidence, string][] = [["request", "原始请求"], ["baseline", "基线响应"], ["candidate", "候选响应"]];

// 原始 JSON 只在有争议时核对，默认折叠、展开才加载；盲评揭晓前只有原始请求
export function Evidence({ runId, jobId, requestOnly = false }: { runId: string; jobId: string; requestOnly?: boolean }) {
  const [data, setData] = useState<Evidence | null>(null);
  const [tab, setTab] = useState<keyof Evidence>("request");
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  function load(open: boolean) {
    if (!open || data) return;
    api<Evidence>(`/experiments/${runId}/results/${jobId}/evidence`).then(setData)
      .catch((cause: unknown) => setError(messageOf(cause)));
  }
  const text = data ? JSON.stringify(data[tab], null, 2) : "";
  return <details className="evidence-details" onToggle={(event) => load((event.target as HTMLDetailsElement).open)}>
    <summary>{requestOnly ? "原始请求（JSON，凭据已过滤；双方响应带模型名，揭晓后可看）" : "原始证据：请求与双方完整响应（JSON，凭据已过滤）"}</summary>
    <Notice error={error} />
    {data ? <>
      <div className="evidence-bar"><div className="segmented">{EVIDENCE_TABS.filter(([key]) => key in data).map(([key, name]) => <button key={key} type="button"
        className={tab === key ? "selected" : ""} onClick={() => { setTab(key); setCopied(false); }}>{name}</button>)}</div>
        <Button size="sm" variant="light" startContent={<Copy size={13} />}
          onPress={() => void navigator.clipboard.writeText(text).then(() => setCopied(true))}>{copied ? "已复制" : "复制"}</Button></div>
      <pre>{data[tab] == null ? "（无）" : text}</pre></> : !error && <p className="muted">正在加载…</p>}
  </details>;
}
