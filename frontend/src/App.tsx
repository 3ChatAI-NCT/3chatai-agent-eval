import { Button, Chip, Spinner } from "@heroui/react";
import { Activity, ClipboardCheck, Database, Layers3, LayoutDashboard, RefreshCw, Settings } from "lucide-react";
import { createContext, useContext, useEffect, useState } from "react";
import { Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { messageOf, api, type ReviewConfig } from "./api";
import { BatchesPage, BatchNewPage, BatchPage, SummaryPage } from "./Batches";
import { GoldenBatchesPage, GoldenBatchNewPage, GoldenBatchPage } from "./GoldenBatches";
import { DatasetEditor, DatasetsPage } from "./Datasets";
import { EvaluationPage, EvaluationsPage, NewEvaluationPage } from "./Evaluations";
import { SECTION_HOME, sectionOf, type Section } from "./Sections";
import { SettingsPage } from "./Settings";
import { SpacePage, SpacesPage, StrategyPage } from "./Spaces";
import { WorkbenchPage } from "./Workbench";

type Capabilities = {
  judge_configured: boolean;
  judge_model: string;
  blind_review: ReviewConfig;
  annotation_configured: boolean;
  registry_configured: boolean;
  environment: string;
};
type Context = { subject: string; capabilities: Capabilities; refresh: () => void;
  markSection: (path: string, section: Section) => void };
const AppContext = createContext<Context | null>(null);

export function useApp() {
  const value = useContext(AppContext);
  if (!value) throw new Error("App context is not ready");
  return value;
}

// 评测详情、新建评测等页面的地址看不出属于哪个栏目，由页面在知道策略后标记导航
export function useNavSection(section: Section | null) {
  const { markSection } = useApp();
  const { pathname } = useLocation();
  useEffect(() => { if (section) markSection(pathname, section); }, [section, pathname]);
}

// 地址本身能判断栏目的直接判断；新建评测看 mode 参数
function sectionPath(pathname: string, search: string) {
  if (/^\/(atomic|batches|summary)/.test(pathname)) return SECTION_HOME.atomic;
  if (/^\/(golden|drafts)/.test(pathname)) return SECTION_HOME.golden;
  const mode = new URLSearchParams(search).get("mode");
  if (pathname === "/evaluations/new" && mode) return SECTION_HOME[sectionOf(mode)];
  return pathname;
}

export function useRemote<T>(load: () => Promise<T>, deps: readonly unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    let current = true;
    setLoading(true);
    load().then((value) => { if (current) { setData(value); setError(""); } })
      .catch((cause: unknown) => { if (current) setError(messageOf(cause)); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
    // The caller lists the identities that determine its loader.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, generation]);
  return { data, error, loading, reload: () => setGeneration((n) => n + 1) };
}

export function Status({ value }: { value: string | null | undefined }) {
  const text: Record<string, string> = {
    active: "已验证", needs_check: "待核验", draft: "草稿", published: "已发布",
    queued: "排队中", running: "运行中", completed: "已完成", succeeded: "成功",
    failed: "失败", not_started: "未开跑", partial: "部分完成", uncertain: "状态待核实", cancelled: "已取消",
    pending: "待复核", in_progress: "复核中", unavailable: "无法判断",
    blind_waiting: "盲评 · 待评测结束", blind_open: "待盲评", revealed: "盲评已揭晓", skipped: "提前揭晓",
  };
  const color = value === "active" || value === "succeeded" || value === "completed"
    ? "success" : value === "failed" || value === "uncertain" ? "danger"
      : value === "partial" || value === "needs_check" || value === "pending" || value === "blind_open" ? "warning"
        : value === "revealed" ? "primary" : "default";
  return <Chip size="sm" variant="flat" color={color}>{text[value || ""] || value || "—"}</Chip>;
}

export function Notice({ error, children }: { error?: string; children?: React.ReactNode }) {
  if (!error && !children) return null;
  return <div className={`notice ${error ? "notice-error" : ""}`} role={error ? "alert" : "status"}>
    {error || children}
  </div>;
}

export function PageHead({ title, description, action }: {
  title: string; description?: string; action?: React.ReactNode;
}) {
  return <div className="page-head"><div><h1>{title}</h1>{description && <p>{description}</p>}</div>{action}</div>;
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="empty">{children}</div>;
}

const nav = [
  { href: "/", label: "工作台", icon: LayoutDashboard },
  { href: "/atomic", label: "单步回放", icon: ClipboardCheck },
  { href: "/golden", label: "逐轮回放", icon: Database },
  { href: "/spaces", label: "评测空间", icon: Layers3 },
  { href: "/settings", label: "设置", icon: Settings },
];

export function App() {
  const location = useLocation();
  const [marked, setMarked] = useState<{ path: string; section: Section } | null>(null);
  const identity = useRemote(async () => {
    const [me, capabilities] = await Promise.all([
      api<{ subject: string }>("/me"), api<Capabilities>("/capabilities"),
    ]);
    return { subject: me.subject, capabilities };
  }, []);
  // 刷新身份与能力时保留页面，只有首次加载才显示启动页
  if (identity.loading && !identity.data) return <div className="startup"><Spinner label="正在连接评测平台" /></div>;
  if (!identity.data) return <div className="startup startup-error">
    <Activity size={26} /><h1>无法进入评测平台</h1>
    <p>{identity.error || "请检查 Gateway 登录与内部成员配置。"}</p>
    <Button onPress={identity.reload}>重试</Button>
  </div>;
  const active = marked?.path === location.pathname ? SECTION_HOME[marked.section]
    : sectionPath(location.pathname, location.search);
  const current = active === "/" ? nav[0] : nav.find((item) => item.href !== "/" && active.startsWith(item.href));
  return <AppContext.Provider value={{ ...identity.data, refresh: identity.reload,
    markSection: (path, section) => setMarked({ path, section }) }}>
    <div className="shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">3</span><div><strong>3chat Eval</strong><small>内部评测工作台</small></div></div>
        <nav aria-label="主导航">{nav.map(({ href, label, icon: Icon }) =>
          <NavLink key={href} to={href} className={`nav-link ${current?.href === href ? "active" : ""}`}>
            <Icon size={18} /><span>{label}</span>
          </NavLink>)}</nav>
        <div className="sidebar-foot"><span className="pulse" />{identity.data.capabilities.environment.toUpperCase()}<br />
          <small>评测只使用隔离 EVAL 空间</small></div>
      </aside>
      <div className="main-column">
        <header className="topbar"><span>{current?.label}</span><span className="top-user">{identity.data.subject}
          <Button isIconOnly size="sm" variant="light" aria-label="刷新身份" onPress={identity.reload}><RefreshCw size={14} /></Button>
        </span></header>
        <main className="page"><Routes>
          <Route path="/" element={<WorkbenchPage />} />
          <Route path="/spaces" element={<SpacesPage />} />
          <Route path="/spaces/:spaceId" element={<SpacePage />} />
          <Route path="/spaces/:spaceId/:strategy" element={<StrategyPage />} />
          <Route path="/atomic" element={<BatchesPage />} />
          <Route path="/atomic/runs" element={<EvaluationsPage section="atomic" />} />
          <Route path="/atomic/datasets" element={<DatasetsPage section="atomic" />} />
          <Route path="/golden" element={<DatasetsPage section="golden" />} />
          <Route path="/golden/batches" element={<GoldenBatchesPage />} />
          <Route path="/golden/batches/new" element={<GoldenBatchNewPage />} />
          <Route path="/golden/batches/:batchId" element={<GoldenBatchPage />} />
          <Route path="/golden/runs" element={<EvaluationsPage section="golden" />} />
          <Route path="/drafts/:draftId" element={<DatasetEditor />} />
          {/* 旧地址保留跳转，已分享的链接不失效 */}
          <Route path="/datasets" element={<Navigate to="/golden" replace />} />
          <Route path="/evaluations" element={<Navigate to="/atomic/runs" replace />} />
          <Route path="/evaluations/new" element={<NewEvaluationPage />} />
          <Route path="/evaluations/:runId" element={<EvaluationPage />} />
          <Route path="/batches/new" element={<BatchNewPage />} />
          <Route path="/batches/:batchId" element={<BatchPage />} />
          <Route path="/summary" element={<SummaryPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Empty>页面不存在。请从左侧导航重新进入。</Empty>} />
        </Routes></main>
      </div>
    </div>
  </AppContext.Provider>;
}
