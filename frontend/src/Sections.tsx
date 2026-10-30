import { Button } from "@heroui/react";
import { Layers, Play } from "lucide-react";
import { Link, NavLink } from "react-router-dom";
import { PageHead } from "./App";

// 单步回放与逐轮回放是两条独立的工作流，各自一个栏目
export type Section = "atomic" | "golden";

export const sectionOf = (mode: string | null | undefined): Section => mode === "golden_regression" ? "golden" : "atomic";
export const SECTION_HOME: Record<Section, string> = { atomic: "/atomic", golden: "/golden" };
export const RUNS: Record<Section, string> = { atomic: "/atomic/runs", golden: "/golden/runs" };
export const SECTION_NAME: Record<Section, string> = { atomic: "单步回放", golden: "逐轮回放" };

const TABS: Record<Section, [string, string][]> = {
  atomic: [["/atomic", "批次"], ["/atomic/runs", "评测记录"], ["/atomic/datasets", "采样数据"]],
  golden: [["/golden/batches", "采样与回放批次"], ["/golden", "参考数据集"], ["/golden/runs", "回放记录"]],
};
const DESCRIPTION: Record<Section, string> = {
  atomic: "采样生产请求原样重放给候选模型，与线上回答成对盲评；可一次对多个空间批量评测，抽样人工盲评后揭晓结论。",
  golden: "采样真实会话并冻结为参考数据，按原始历史独立回放每一轮；依据空间规则检查智能体表现。参考回复不代表唯一正确答案。",
};

export function SectionHead({ section }: { section: Section }) {
  const action = section === "atomic"
    ? <div className="head-actions">
      <Button as={Link} to="/batches/new" variant="flat" startContent={<Layers size={15} />}>批量评测</Button>
      <Button as={Link} to="/evaluations/new?mode=atomic_replay" color="primary" startContent={<Play size={15} />}>创建评测</Button></div>
    : <div className="head-actions"><Button as={Link} to="/evaluations/new?mode=golden_regression" variant="flat">用已有数据回放</Button>
      <Button as={Link} to="/golden/batches/new" color="primary" startContent={<Layers size={15} />}>批量采样并回放</Button></div>;
  return <>
    <PageHead title={SECTION_NAME[section]} description={DESCRIPTION[section]} action={action} />
    <nav className="segmented section-tabs" aria-label={`${SECTION_NAME[section]}页签`}>{TABS[section].map(([href, label]) =>
      <NavLink key={href} to={href} end className={({ isActive }) => isActive ? "selected" : ""}>{label}</NavLink>)}</nav>
  </>;
}
