import { Icon } from "../icons";
import type { EngineCapabilities, EngineCapabilityItem } from "../protocol";

interface Props {
  open: boolean;
  report: EngineCapabilities | null;
  loading: boolean;
  onRefresh: () => void;
  onManagePlugin: (item: EngineCapabilityItem, action: "install" | "uninstall") => void;
  onClose: () => void;
}

const LABELS: Record<EngineCapabilityItem["kind"], string> = {
  skill: "技能", plugin: "插件", app: "应用", mcp: "MCP",
};

function itemMeta(item: EngineCapabilityItem): string {
  const bits: string[] = [];
  if (item.installed !== undefined) bits.push(item.installed ? "已安装" : "未安装");
  if (item.enabled !== undefined) bits.push(item.enabled ? "已启用" : "已停用");
  if (item.status) bits.push(item.status);
  if (item.scope) bits.push(item.scope);
  if (item.tool_count !== undefined) bits.push(`${item.tool_count} 个工具`);
  if (item.resource_count !== undefined) bits.push(`${item.resource_count} 个资源`);
  return bits.join(" · ");
}

export function CapabilitiesSheet({ open, report, loading, onRefresh, onManagePlugin, onClose }: Props) {
  if (!open) return null;
  const grouped = (["skill", "plugin", "app", "mcp"] as const).map((kind) => ({
    kind, items: report?.items.filter((item) => item.kind === kind) ?? [],
  })).filter((group) => group.items.length > 0);
  return <>
    <div className="scrim show" onClick={onClose} />
    <section className="capabilities-sheet" role="dialog" aria-modal="true" aria-label="Agent 能力">
      <header>
        <div><b>Agent 能力</b><small>来自当前引擎的实时目录</small></div>
        <div className="capabilities-head-actions">
          <button className="iconbtn" onClick={onRefresh} aria-label="刷新" title="刷新"><Icon name="refresh" /></button>
          <button className="iconbtn" onClick={onClose} aria-label="关闭"><Icon name="close" /></button>
        </div>
      </header>
      <div className="capabilities-body">
        {loading && !report && <div className="capabilities-empty">正在读取技能、插件、应用与 MCP…</div>}
        {report?.notes?.map((note) => <div className="capabilities-note" key={note}>{note}</div>)}
        {report?.errors?.length ? <div className="capabilities-errors">
          <b>部分目录暂不可用</b>{report.errors.map((error) => <span key={error}>{error}</span>)}
        </div> : null}
        {!loading && report && !grouped.length && <div className="capabilities-empty">当前空间没有启用扩展能力。</div>}
        {grouped.map((group) => <section className="capabilities-group" key={group.kind}>
          <h3>{LABELS[group.kind]}<span>{group.items.length}</span></h3>
          {group.items.map((item) => <article key={`${item.kind}:${item.id}`}>
            <div><b>{item.name}</b>{item.description && <p>{item.description}</p>}</div>
            <div className="capabilities-item-actions">
              <small>{itemMeta(item)}</small>
              {item.kind === "plugin" && item.installed !== undefined && <button
                onClick={() => onManagePlugin(item, item.installed ? "uninstall" : "install")}>
                {item.installed ? "卸载" : "安装"}
              </button>}
              {item.kind === "app" && item.install_url && item.status !== "accessible" && <a
                href={item.install_url} target="_blank" rel="noopener noreferrer">连接</a>}
            </div>
          </article>)}
        </section>)}
      </div>
    </section>
  </>;
}
