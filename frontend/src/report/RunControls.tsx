import { Button } from "@heroui/react";
import { useRef, useState } from "react";
import { isFinished, messageOf, submit, type Experiment, type Report } from "../api";

export const abnormalCount = (report: Report) => report.execution_failed + report.uncertain + report.judge_failed;

// 运行中可取消；失败或部分完成时只补跑异常项。报告页与盲评页共用
export function RunControls({ runId, run, report, reload }: {
  runId: string; run: Experiment; report: Report; reload: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [error, setError] = useState("");
  const retryKey = useRef<string | null>(null);
  const abnormal = abnormalCount(report);
  const candidateSource = (run.manifest?.config as { candidate_source?: string } | undefined)?.candidate_source;
  const golden = run.mode === "golden_regression";
  const canRetry = ["failed", "uncertain"].includes(run.status) && abnormal > 0
    && (golden ? report.execution_failed === 0 && report.uncertain === 0
      : report.fixture || candidateSource === "gateway") && !run.archived_at;
  async function cancel() {
    setBusy(true); setError("");
    try { await submit(`/experiments/${runId}/cancel`, {}); reload(); }
    catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  async function retryFailed() {
    setRetrying(true); setError("");
    try {
      retryKey.current ||= crypto.randomUUID();
      await submit(`/experiments/${runId}/retry-failed`, {}, { "Idempotency-Key": retryKey.current });
      retryKey.current = null;
      reload();
    } catch (cause) { setError(messageOf(cause)); }
    finally { setRetrying(false); }
  }
  return <>
    {canRetry && <Button variant="flat" color="warning" isLoading={retrying}
      onPress={() => void retryFailed()}>{golden ? "重试评分异常" : "补跑失败项并继续判分"}（{abnormal}）</Button>}
    {!isFinished(run.status) && <Button variant="light" color="danger" isLoading={busy} onPress={() => void cancel()}>取消评测</Button>}
    {canRetry && <small className="muted">{golden ? "只重新评分异常项，沿用原回答和已冻结标准。" : "只补跑异常任务；上游限制未解除时仍可能失败。"}</small>}
    {error && <small className="danger-text">{error}</small>}
  </>;
}
