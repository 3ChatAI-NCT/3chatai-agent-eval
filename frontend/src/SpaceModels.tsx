import { Button, Chip, Input, Modal, ModalBody, ModalContent, ModalFooter, ModalHeader, Spinner } from "@heroui/react";
import { CircleAlert, CircleCheck } from "lucide-react";
import { useState } from "react";
import { Notice } from "./App";
import {
  api, messageOf, submit, type Space, type SpaceModel, type SpaceModelResult, type SpaceModels, type SpaceProbe,
} from "./api";

const MODEL_NAME = /^[A-Za-z0-9._:/-]{1,100}$/;
// 批量测试同时进行的空间数
const PROBE_CONCURRENCY = 4;

export const modelLabel = (model: string | null | undefined) => model || "默认";

export const loadSpaceModels = () => api<SpaceModels>("/space-models");

export const probeSpace = (spaceId: string) => submit<SpaceProbe>(`/spaces/${spaceId}/model-probe`, {});

export function modelIndex(data: SpaceModels | null | undefined) {
  return new Map((data?.items || []).map((item) => [item.space_id, item]));
}

// 不能切换的原因：执行中切换会让同一评测前后用上两个模型；Judge 或草稿模型正好是被替换的模型时会一起被换掉
export function lockReason(item: SpaceModel | undefined) {
  if (!item) return "";
  if (item.protected) return "会连 Judge 模型一起替换";
  return item.locked ? "逐轮回放执行中" : "";
}

// 逐个测试，最多 PROBE_CONCURRENCY 个同时进行；单个失败不影响其他空间
export async function probeAll(spaceIds: string[], onResult: (spaceId: string, result: SpaceProbe | string) => void) {
  const queue = [...spaceIds];
  async function next(): Promise<void> {
    const spaceId = queue.shift();
    if (!spaceId) return;
    try { onResult(spaceId, await probeSpace(spaceId)); }
    catch (cause) { onResult(spaceId, messageOf(cause)); }
    return next();
  }
  await Promise.all(Array.from({ length: Math.min(PROBE_CONCURRENCY, queue.length) }, next));
}

const seconds = (ms: number) => `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;

export function ProbeChip({ probe, running, error }: { probe?: SpaceProbe | null; running?: boolean; error?: string }) {
  if (running) return <Chip size="sm" variant="flat" color="primary" startContent={<Spinner size="sm" color="current" />}>测试中</Chip>;
  if (error) return <Chip size="sm" variant="flat" color="danger">测试失败</Chip>;
  if (!probe) return null;
  return probe.ok ? <Chip size="sm" variant="flat" color="success">可用 · {seconds(probe.latency_ms)}</Chip>
    : <Chip size="sm" variant="flat" color="danger">不可用</Chip>;
}

export function ProbeResult({ probe, running, error }: { probe?: SpaceProbe | null; running?: boolean; error?: string }) {
  if (running) return <div className="probe-result probe-running probe-inline"><Spinner size="sm" />
    <span>正在以 EVAL 租户身份请求被替换的模型，通常 1–5 秒，最长 30 秒…</span></div>;
  if (error) return <div className="probe-result probe-failed probe-inline"><CircleAlert size={16} /><span>测试请求失败：{error}</span></div>;
  if (!probe) return <p className="muted">点击「测试」以 EVAL 租户身份发一次极小的请求，确认 Agent 实际能调通当前模型；结果不保存。</p>;
  return <div className={`probe-result ${probe.ok ? "probe-ok" : "probe-failed"}`}>
    <div className="probe-stats">
      <div><span>实际模型</span><strong className="mono">{probe.model || "—"}</strong></div>
      <div><span>耗时</span><strong>{seconds(probe.latency_ms)}</strong></div>
      <div><span>Tokens（入 / 出）</span><strong>{probe.input_tokens ?? "—"} / {probe.output_tokens ?? "—"}</strong></div>
    </div>
    <p className="probe-note">{probe.ok ? <CircleCheck size={15} /> : <CircleAlert size={15} />}
      {probe.ok ? <span>{probe.detail || `调用成功，回复「${probe.reply || "（空）"}」`}</span> : <span>{probe.detail}</span>}</p>
  </div>;
}

type Saved = { result: SpaceModelResult; target: string | null };
type Probes = Record<string, SpaceProbe | string | undefined>;

function savedMessage({ result, target }: Saved) {
  return target ? `已将 ${result.changes.length} 个空间的执行模型设为 ${target}。`
    : `已将 ${result.changes.length} 个空间恢复默认模型。`;
}

export function SpaceModelDialog({ spaces, models, fromModel, reset = false, onClose, onSaved }: {
  spaces: Space[]; models: Map<string, SpaceModel>; fromModel: string; reset?: boolean;
  onClose: () => void; onSaved: (message: string) => void;
}) {
  const single = spaces.length === 1 ? models.get(spaces[0].id)?.model : null;
  const [value, setValue] = useState(reset ? "" : single || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState<Saved | null>(null);
  const [probes, setProbes] = useState<Probes>({});
  const typed = value.trim();
  // 填写默认模型本身等于恢复默认
  const target = reset || !typed || typed === fromModel ? null : typed;
  const invalid = !reset && !!typed && !MODEL_NAME.test(typed);
  const shared = spaces.filter((space) => models.get(space.id)?.shared).map((space) => space.name);
  const names = new Map(spaces.map((space) => [space.id, space.name]));
  const testing = spaces.some((space) => saved && !(space.id in probes));
  const failed = spaces.filter((space) => {
    const probe = probes[space.id];
    return typeof probe === "string" || probe?.ok === false;
  });

  async function test(ids: string[]) {
    setProbes((current) => Object.fromEntries(Object.entries(current).filter(([id]) => !ids.includes(id))));
    await probeAll(ids, (id, probe) => setProbes((current) => ({ ...current, [id]: probe })));
  }
  async function save() {
    setBusy(true); setError("");
    try {
      const result = await api<SpaceModelResult>("/space-models", {
        method: "PUT", body: JSON.stringify({ space_ids: spaces.map((space) => space.id), model: target }),
      });
      setSaved({ result, target });
      void test(spaces.map((space) => space.id));
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  // 测试失败时按各空间切换前的值分组写回
  async function restore() {
    if (!saved) return;
    setBusy(true); setError("");
    try {
      const groups = new Map<string | null, string[]>();
      for (const change of saved.result.changes.filter((item) => failed.some((space) => space.id === item.space_id))) {
        groups.set(change.previous_model, [...(groups.get(change.previous_model) || []), change.space_id]);
      }
      for (const [model, ids] of groups) {
        await api<SpaceModelResult>("/space-models", { method: "PUT", body: JSON.stringify({ space_ids: ids, model }) });
      }
      onSaved(`新模型测试不可用，已将 ${failed.length} 个空间恢复为原模型。`);
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  const close = () => { if (busy) return; if (saved) onSaved(savedMessage(saved)); else onClose(); };

  return <Modal isOpen onClose={close} size="lg">
    <ModalContent>
      <ModalHeader>{reset ? "恢复默认模型" : "设置执行模型"}{spaces.length > 1 ? ` · ${spaces.length} 个空间` : ` · ${spaces[0].name}`}</ModalHeader>
      {!saved ? <ModalBody>
        <p className="muted">EVAL 空间的 Agent 请求 <span className="mono">{fromModel}</span> 时，网关替换为{reset ? "它本身（不替换）" : "下面填写的模型"}。只影响 EVAL 租户，不影响来源空间与生产。</p>
        <Notice error={error} />
        {shared.length > 0 && <Notice>{shared.join("、")} 与评测平台同租户：平台经网关请求 {fromModel} 的调用（例如单步回放选它作网关候选）也会被替换；Judge 不受影响。</Notice>}
        {!reset && <Input label="执行模型" placeholder="例如：qwen3.7-plus" value={value} onValueChange={setValue} autoFocus
          classNames={{ input: "mono" }} isInvalid={invalid}
          errorMessage={invalid ? "只能包含字母、数字和 . _ : / -，最多 100 个字符" : undefined}
          description={`填写网关能识别的模型名；留空或填 ${fromModel} 即恢复默认`} />}
        <div className="selection-box">{spaces.map((space) => {
          const current = models.get(space.id)?.model ?? null;
          return <span key={space.id}>{space.name}：{modelLabel(current)} → {modelLabel(target)}{current === target ? "（不变）" : ""}</span>;
        })}</div>
        <p className="hint">确认后会立即以各 EVAL 租户身份测试一次，确认 Agent 实际能调通新模型。</p>
      </ModalBody> : <ModalBody>
        <Notice error={error} />
        <ol className="probe-steps">
          <li className="done"><CircleCheck size={16} /><span>已写入网关映射</span><small>{modelLabel(saved.target)}</small></li>
          <li className={saved.result.mismatched.length ? "failed" : "done"}>
            {saved.result.mismatched.length ? <CircleAlert size={16} /> : <CircleCheck size={16} />}
            <span>网关回查{saved.result.mismatched.length ? "不一致" : "一致"}</span>
            {!!saved.result.mismatched.length && <small>{saved.result.mismatched.map((id) => names.get(id) || id).join("、")}</small>}</li>
          <li className={testing ? "running" : failed.length ? "failed" : "done"}>
            {testing ? <Spinner size="sm" /> : failed.length ? <CircleAlert size={16} /> : <CircleCheck size={16} />}
            <span>{testing ? "正在测试调用…" : failed.length ? `${failed.length} 个空间测试不可用` : "测试调用全部可用"}</span></li>
        </ol>
        <div className="probe-rows">{spaces.map((space) => {
          const probe = probes[space.id];
          return <div key={space.id}><span>{space.name}</span>
            <ProbeChip running={!(space.id in probes)} probe={typeof probe === "string" ? null : probe}
              error={typeof probe === "string" ? probe : undefined} />
            <small className="muted">{typeof probe === "string" ? probe
              : probe ? (probe.ok ? `实际模型 ${probe.model || "—"}` : probe.detail) : ""}</small></div>;
        })}</div>
        {saved.result.unknown_model && <Notice>网关模型列表里没有 {saved.target}，如果测试失败，请先确认拼写。</Notice>}
        {!testing && failed.length > 0 && <div className="notice notice-error" role="alert">
          新模型在 {failed.map((space) => space.name).join("、")} 不可用，逐轮回放会全部失败。建议恢复为原模型。</div>}
      </ModalBody>}
      <ModalFooter>
        {!saved ? <>
          <Button variant="light" onPress={onClose} isDisabled={busy}>取消</Button>
          <Button color="primary" isLoading={busy} isDisabled={invalid} onPress={() => void save()}>
            {reset ? "确认恢复" : "确认设置"}</Button>
        </> : !testing && failed.length > 0 ? <>
          <Button variant="light" onPress={close} isDisabled={busy}>保留并关闭</Button>
          <Button variant="flat" onPress={() => void test(failed.map((space) => space.id))} isDisabled={busy}>重新测试</Button>
          <Button color="primary" isLoading={busy} onPress={() => void restore()}>恢复为原模型</Button>
        </> : <Button color="primary" onPress={close} isDisabled={testing}>完成</Button>}
      </ModalFooter>
    </ModalContent>
  </Modal>;
}
