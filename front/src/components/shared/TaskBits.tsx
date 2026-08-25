import { cn } from "@/lib/utils";
import type { GatewayStatus, GatewayToken, GatewayTask } from "@/lib/api";

function formatClock(v?: number | string | null) {
  if (!v) return "—";
  try {
    const d = typeof v === "number" ? new Date(v * 1000) : new Date(v);
    return isNaN(d.getTime()) ? "—" : d.toLocaleString();
  } catch {
    return "—";
  }
}

export function RunBadge({
  actions,
}: {
  actions?: GatewayTask["actions"] | null;
}) {
  if (!actions) return null;
  const label =
    actions.status === "in_progress"
      ? "运行中"
      : actions.status === "queued"
        ? "排队中"
        : actions.conclusion === "success"
          ? "成功"
          : actions.conclusion === "failure"
            ? "失败"
            : actions.conclusion || actions.status || "";
  const tone =
    actions.status === "in_progress" || actions.status === "queued"
      ? "bg-amber-50 text-amber-700"
      : actions.conclusion === "success"
        ? "bg-emerald-50 text-emerald-700"
        : actions.conclusion && actions.conclusion !== "success"
          ? "bg-red-50 text-red-700"
          : "bg-slate-100 text-slate-600";
  return (
    <a
      href={actions.html_url || `https://github.com/Ryufeiron/grok-register/actions/runs/${actions.run_id}`}
      target="_blank"
      rel="noreferrer"
      className={cn("rounded-full px-2 py-0.5 text-xs font-medium hover:underline", tone)}
      title={`Run ${actions.run_id} · ${actions.created_at || ""}`}
    >
      Run {actions.run_id} · {label}
      {actions.created_at ? ` · ${formatClock(actions.created_at)}` : ""}
    </a>
  );
}

export function LogTail({ lines, max = 12 }: { lines?: string[]; max?: number }) {
  if (!lines || lines.length === 0) {
    return <div className="text-xs text-slate-400">暂无远程注册输出（触发注册后此处同步日志尾部）</div>;
  }
  return (
    <pre className="max-h-40 overflow-auto rounded-lg bg-slate-950 p-3 text-[11px] leading-5 text-slate-300">
      {lines.slice(-max).join("\n")}
    </pre>
  );
}

export function PoolSummaryTiles({
  pool,
}: {
  pool?: (Pick<GatewayStatus, "tokens_total" | "tokens_healthy"> & { tokens?: GatewayToken[] }) | null;
}) {
  const total = pool?.tokens_total ?? 0;
  const healthy = pool?.tokens_healthy ?? 0;
  const quotaSum = (pool?.tokens ?? []).reduce((acc: number, t: GatewayToken) => acc + (t.quota_remaining || 0), 0);
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <div className="rounded-xl border border-slate-200 bg-slate-50/70 px-3 py-3">
        <div className="text-xs text-slate-500">Token 池</div>
        <div className="mt-1 text-sm font-semibold tabular-nums text-slate-900">
          {healthy} 可用 / {total}
        </div>
      </div>
      <div className="rounded-xl border border-slate-200 bg-slate-50/70 px-3 py-3">
        <div className="text-xs text-slate-500">剩余总额度</div>
        <div className="mt-1 text-sm font-semibold tabular-nums text-slate-900">
          {(quotaSum / 10000).toFixed(1)} 万
        </div>
      </div>
    </div>
  );
}
