import { Button } from "@heroui/react";
import { type FormatCounts, type Side } from "../api";

const ratio = (count: number, total: number) => total ? `${(100 * count / total).toFixed(2)}%` : "—";

export function ResponseFormatTable({ rows, onFocus }: {
  rows: { name: string; counts: FormatCounts; side?: Side }[];
  onFocus?: (side: Side) => void;
}) {
  if (!rows.length) return null;
  return <div className="response-format-summary"><h3>响应格式检查</h3>
    <p className="hint">按冻结输入中的 IM JSON Schema 确定性校验，格式违规标为重要问题。比例分母为要求此协议且已取得最终回复的次数；工具调用、没有此协议的请求不计入分母。此检查不改写历史 AI 结论。</p>
    <div className="table-scroll"><table><thead><tr><th>对象</th><th>响应格式问题 / 已检查</th><th>问题比例</th><th>messages 返回字符串</th><th>非严格 JSON</th></tr></thead><tbody>
      {rows.map(({ name, counts, side }) => <tr key={name}>
        <td>{name}</td><td className={counts.invalid ? "danger-text" : ""}>
          {onFocus && side && counts.invalid ? <Button size="sm" variant="light" color="danger" onPress={() => onFocus(side)}>{counts.invalid} / {counts.checked} · 查看</Button>
            : `${counts.invalid} / ${counts.checked}`}</td>
        <td>{ratio(counts.invalid, counts.checked)}</td>
        <td>{counts.messages_string} · {ratio(counts.messages_string, counts.checked)}</td>
        <td>{counts.invalid_json}</td>
      </tr>)}
    </tbody></table></div>
  </div>;
}
