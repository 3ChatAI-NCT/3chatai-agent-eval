import { Button } from "@heroui/react";
import { useEffect, useRef, useState } from "react";
import { messageOf } from "../api";
import { ISSUE_KIND } from "../atomic";
import { shortcutsBlocked } from "./shortcuts";

export type Draft = { label: string; issue_kind: string; note: string };
export type SavedDraft = { label: string; issue_kind?: string; note: string; revision: number };

// 报告页复核与盲评共用：choices 的顺序与页面左右两侧一致，数字键 1–4 对应从左到右
export function ReviewBar({ review, finished, choices, issueKinds = ISSUE_KIND, colored, draft: pendingDraft, onDraftChange, submitDraft, onSaved }: {
  review: SavedDraft | null; finished: boolean; choices: [string, string][]; issueKinds?: [string, string][];
  colored: boolean; draft?: Draft; onDraftChange: (draft: Draft) => void;
  submitDraft: (draft: Draft) => Promise<number>; onSaved: (review: SavedDraft, advance: boolean) => void;
}) {
  const barRef = useRef<HTMLDivElement>(null);
  const draft = pendingDraft || { label: review?.label || "", issue_kind: review?.issue_kind || "none", note: review?.note || "" };
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const needsNote = (draft.issue_kind !== "none" || ["U", "unavailable"].includes(draft.label)) && !draft.note.trim();
  const unchanged = !!review && draft.label === review.label && draft.issue_kind === (review.issue_kind || "none")
    && draft.note === review.note;
  function update(patch: Partial<Draft>) {
    onDraftChange({ ...draft, ...patch });
    setError("");
  }
  async function save(advance: boolean) {
    if (!finished || !draft.label || needsNote || saving) return;
    if (unchanged && review) { onSaved(review, advance); return; }
    setSaving(true); setError("");
    try {
      onSaved({ ...draft, revision: await submitDraft(draft) }, advance);
    } catch (cause) {
      const message = messageOf(cause);
      setError(/版本冲突|证据已变化/.test(message) ? "这条已在其他窗口或由他人修改，请刷新页面后再复核" : message);
    } finally { setSaving(false); }
  }
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && !shortcutsBlocked(null)
        && event.target instanceof Node && barRef.current?.contains(event.target)) {
        event.preventDefault(); void save(true); return;
      }
      if (shortcutsBlocked(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
      const choice = choices[Number(event.key) - 1];
      if (!choice) return;
      event.preventDefault();
      update({ label: choice[0] });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const status = !finished ? "评测结束后可复核" : saving ? "保存中…" : error ? `保存失败：${error}`
    : needsNote && draft.label ? "请补充判断依据" : pendingDraft ? "未提交草稿" : review ? `已保存 · 第 ${review.revision} 版` : "选择结论并提交";
  return <div className="review-bar" ref={barRef}>
    <div className="review-bar-head"><h4>人工复核</h4><span className={`save-state ${error ? "danger-text" : ""}`}>{status}</span></div>
    <div className="choice-row">{choices.map(([value, text], index) => <button key={value} type="button" disabled={!finished || saving}
      className={`choice ${draft.label === value ? "selected" : ""} ${colored ? `choice-${value}` : ""}`}
      onClick={() => update({ label: value })}><kbd>{index + 1}</kbd>{text}</button>)}</div>
    <div className="issue-row"><span className="muted">问题类型</span>{issueKinds.map(([value, text]) => <button key={value} type="button" disabled={!finished || saving}
      className={`tag ${draft.issue_kind === value ? "selected" : ""}`} onClick={() => update({ issue_kind: value })}>{text}</button>)}</div>
    <textarea className={`note ${needsNote && draft.label ? "note-required" : ""}`} rows={2} disabled={!finished || saving} value={draft.note}
      placeholder={draft.issue_kind !== "none" || ["U", "unavailable"].includes(draft.label)
        ? "必填：说明证据、无法判断的原因或处理建议" : "可选：记录业务判断依据"}
      onChange={(event) => update({ note: event.target.value })} />
    <div className="review-actions"><Button size="sm" variant="flat" isDisabled={!finished || !draft.label || needsNote || saving}
      isLoading={saving} onPress={() => void save(false)}>保存当前复核</Button>
      <Button size="sm" color="primary" isDisabled={!finished || !draft.label || needsNote || saving}
        onPress={() => void save(true)}>保存并下一条</Button></div>
  </div>;
}
