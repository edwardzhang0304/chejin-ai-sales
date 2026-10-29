import { useCallback, useEffect, useState } from "react";
import { useAutoRefresh } from "../../shared/hooks/useAutoRefresh";
import {
  RefreshCw,
  UserRound,
  UserPlus,
  UsersRound,
  MessageCircle,
  BadgeCheck,
  Percent,
  Clock3,
  Pause,
  CircleAlert,
  WifiOff,
  Unplug,
  CarFront,
  Archive,
  Banknote,
  CircleHelp,
  CalendarDays,
} from "lucide-react";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  CartesianGrid,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
} from "recharts";
import { formatBusinessError } from "../../shared/api/client";
import {
  getDashboard,
  type DashboardData,
  type Metric,
  type MetricKey,
} from "./api";

const periods = ["今日", "昨日", "近7天", "近30天", "自定义"] as const;
type Period = (typeof periods)[number];
export function beijingDate(now = new Date()) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now);
}
export function dateRange(
  period: Period,
  custom: [string, string],
  today: string,
): [string, string] {
  if (period === "自定义") return custom;
  const shift = (days: number) =>
    new Date(Date.parse(`${today}T00:00:00Z`) - days * 86400000)
      .toISOString()
      .slice(0, 10);
  return period === "昨日"
    ? [shift(1), shift(1)]
    : [shift(period === "近7天" ? 6 : period === "近30天" ? 29 : 0), today];
}
const metrics: Array<{
  key: MetricKey;
  name: string;
  icon: typeof UserPlus;
  definition: string;
}> = [
  {
    key: "new_customers",
    name: "新增客户",
    icon: UserPlus,
    definition: "所选期间首次创建的去重客户；重复录入不计。",
  },
  {
    key: "total_customers",
    name: "客户总量",
    icon: UsersRound,
    definition: "截至所选结束时点累计创建的去重客户；无效客户不扣减。",
  },
  {
    key: "friend_passes",
    name: "微信通过数",
    icon: MessageCircle,
    definition: "所选期间首次可靠确认通过好友申请的客户数。",
  },
  {
    key: "friend_pass_rate",
    name: "微信通过率",
    icon: Percent,
    definition: "所选期间成功发出邀请的同一批客户，截至统计时点的通过比例。",
  },
  {
    key: "communication_customers",
    name: "沟通客户",
    icon: MessageCircle,
    definition: "AI或人工实际发送成功消息的去重客户，客户未回复也计入。",
  },
  {
    key: "effective_customers",
    name: "有效沟通客户",
    icon: BadgeCheck,
    definition:
      "同筛选沟通客户中，在连续24小时内发出至少2条真实消息，并在所选期间达到条件的客户。",
  },
  {
    key: "effective_rate",
    name: "有效沟通率",
    icon: Percent,
    definition: "同一筛选条件下，有效沟通客户数 / 沟通客户数。",
  },
  {
    key: "human_response_seconds",
    name: "人工平均首次响应时长",
    icon: Clock3,
    definition:
      "首条待响应客户消息至首条实际人工回复，按已回复轮次加权；AI回复不重置计时。",
  },
];
const states = [
  ["running", "接单中", UserRound],
  ["paused", "暂停接单", Pause],
  ["faulted", "故障停止", CircleAlert],
  ["offline", "离线", WifiOff],
  ["unbound", "未绑定 Worker", Unplug],
] as const;
export function formatMetric(item: Metric | undefined, key: string): string {
  if (!item || item.reason || item.value === null) return "—";
  const value = Number(item.value);
  if (key.endsWith("rate")) return `${value.toFixed(2)}%`;
  if (key === "human_response_seconds") {
    const seconds = Math.round(value);
    return `${Math.floor(seconds / 60)}分 ${seconds % 60}秒`;
  }
  if (key === "amount")
    return `¥${value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return value.toLocaleString("zh-CN");
}
function PeriodFilter({
  value,
  onChange,
}: {
  value: Period;
  onChange: (value: Period) => void;
}) {
  return (
    <div className="period-filter" role="group" aria-label="统计日期">
      {periods.map((p) => (
        <button
          type="button"
          className={p === value ? "active" : ""}
          key={p}
          onClick={() => onChange(p)}
        >
          {p}
        </button>
      ))}
    </div>
  );
}
function Dates({
  period,
  value,
  onChange,
  label,
  today,
}: {
  period: Period;
  value: [string, string];
  onChange: (range: [string, string]) => void;
  label: string;
  today: string;
}) {
  return period === "自定义" ? (
    <div className="custom-range">
      <input
        type="date"
        value={value[0]}
        max={value[1] || today}
        onChange={(e) => onChange([e.target.value, value[1]])}
        aria-label={`${label}开始日期`}
      />
      <span>至</span>
      <input
        type="date"
        value={value[1]}
        min={value[0]}
        max={today}
        onChange={(e) => onChange([value[0], e.target.value])}
        aria-label={`${label}结束日期`}
      />
    </div>
  ) : null;
}
function SalesFilter({
  value,
  onChange,
  options,
  label = "筛选销售人员",
  unassigned = false,
}: {
  value: string;
  onChange: (v: string) => void;
  options: DashboardData["sales_options"];
  label?: string;
  unassigned?: boolean;
}) {
  return (
    <select
      aria-label={label}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="all">全部销售</option>
      {options.map((p) => (
        <option key={p.id} value={p.id}>
          {p.name}
          {p.enabled ? "" : "（已停用）"}
        </option>
      ))}
      {unassigned && <option value="unassigned">未分配</option>}
    </select>
  );
}

export function DashboardPage() {
  const today = beijingDate();
  const [period, setPeriod] = useState<Period>("今日"),
    [vehiclePeriod, setVehiclePeriod] = useState<Period>("今日");
  const [dates, setDates] = useState<[string, string]>([today, today]),
    [vehicleDates, setVehicleDates] = useState<[string, string]>([
      today,
      today,
    ]);
  const [sales, setSales] = useState("all"),
    [statusSales, setStatusSales] = useState("all"),
    [mode, setMode] = useState("all");
  const [revision, setRevision] = useState(0),
    [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true),
    [error, setError] = useState<string | null>(null);
  const [options, setOptions] = useState<DashboardData["sales_options"]>([]);
  const [start, end] = dateRange(period, dates, today),
    [vehicleStart, vehicleEnd] = dateRange(vehiclePeriod, vehicleDates, today);
  const invalidDates =
    !start ||
    !end ||
    start > end ||
    end > today ||
    !vehicleStart ||
    !vehicleEnd ||
    vehicleStart > vehicleEnd ||
    vehicleEnd > today;
  const refresh = useCallback(async (signal: AbortSignal) => {
    const result = await getDashboard({
      start, end, vehicle_start: vehicleStart, vehicle_end: vehicleEnd,
      sales_id: sales, mode, status_sales_id: statusSales,
    }, signal);
    if (!signal.aborted) {
      setData(result);
      setOptions(result.sales_options);
    }
  }, [start, end, vehicleStart, vehicleEnd, sales, mode, statusSales]);
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setError(null);
    if (invalidDates) {
      setLoading(false);
      return () => controller.abort();
    }
    setLoading(true);
    void refresh(controller.signal)
      .catch((reason) => {
        if (!controller.signal.aborted)
          setError(formatBusinessError(reason, "统计服务暂不可用，请重试。"));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [refresh, revision, invalidDates]);
  // Share the existing visible-page refresh scheduler; failures retain the
  // last successful snapshot and its timestamp, without clearing the page.
  useAutoRefresh(refresh, !loading && !invalidDates && data !== null);
  return (
    <div
      className={`dashboard ${loading ? "is-loading" : ""}`}
      aria-busy={loading}
    >
      <header className="dashboard-heading">
        <div>
          <h1>数据看板</h1>
        </div>
        <div className="dashboard-stamp">
          <button
            type="button"
            title="刷新数据"
            aria-label="刷新数据"
            disabled={loading || invalidDates}
            onClick={() => setRevision((v) => v + 1)}
          >
            <RefreshCw size={14} />
          </button>
          <span>
            {data
              ? `更新于 ${new Date(data.as_of).toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai" })}`
              : loading
                ? "正在加载"
                : "尚未取得数据"}
          </span>
          <i />
          <CalendarDays size={15} />
          <span>{today} · 北京时间</span>
        </div>
      </header>
      {error && (
        <div className="inline-alert error" role="alert">
          {error}
          <button type="button" onClick={() => setRevision((v) => v + 1)}>
            重新加载
          </button>
        </div>
      )}
      {invalidDates && (
        <div className="inline-alert error" role="alert">
          请填写有效日期，结束日期不能晚于今天。
        </div>
      )}
      <section className="ai-strip" aria-label="AI 销售实时状态">
        <div className="ai-strip-title">
          <strong>AI 销售 · 实时</strong>
          <SalesFilter
            value={statusSales}
            onChange={setStatusSales}
            options={options}
            label="实时状态销售筛选"
          />
        </div>
        {states.map(([key, name, Icon]) => (
          <div className="ai-state" key={key}>
            <span
              className={`metric-icon ${key === "faulted" ? "danger-icon" : ""}`}
            >
              <Icon size={19} />
            </span>
            <div>
              <span>{name}</span>
              <strong>
                {data?.realtime.counts[key] ?? "—"}
                <small> 人</small>
              </strong>
            </div>
          </div>
        ))}
      </section>
      {!!data?.realtime.unavailable && (
        <p className="dashboard-coverage" role="status">
          另有 {data.realtime.unavailable} 名销售状态暂不可用，未计入上述分组。
        </p>
      )}
      <section className="dashboard-band customer-band">
        <header className="dashboard-band-head">
          <div className="customer-heading">
            <h2>客户数据</h2>
            <div className="segmented" role="group" aria-label="沟通方式">
              {[
                ["all", "全部"],
                ["ai", "AI"],
                ["human", "人工"],
              ].map(([value, label]) => (
                <button
                  type="button"
                  key={value}
                  title="仅筛选沟通客户、有效沟通及有效沟通率"
                  className={mode === value ? "active" : ""}
                  onClick={() => setMode(value)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          <div className="dashboard-filters">
            <PeriodFilter value={period} onChange={setPeriod} />
            <SalesFilter
              value={sales}
              onChange={setSales}
              options={options}
              unassigned
            />
          </div>
        </header>
        <Dates
          period={period}
          value={dates}
          onChange={setDates}
          label="客户"
          today={today}
        />
        <div className="customer-metrics">
          {metrics.map(({ key, name, icon: Icon, definition }) => (
            <article className="customer-metric" key={key}>
              <span className="metric-icon">
                <Icon size={20} />
              </span>
              <div>
                <div className="metric-label">
                  {name}
                  <button
                    type="button"
                    title={definition}
                    aria-label={`${name}统计口径`}
                    className="metric-help"
                  >
                    <CircleHelp size={12} />
                  </button>
                </div>
                <strong title={data?.metrics[key].reason || undefined}>
                  {formatMetric(data?.metrics[key], key)}
                  {!key.endsWith("rate") &&
                    key !== "human_response_seconds" && <small> 人</small>}
                </strong>
                {data?.metrics[key].reason && (
                  <small className="metric-unavailable">覆盖不足</small>
                )}
              </div>
            </article>
          ))}
        </div>
      </section>
      <div className="dashboard-middle">
        <section className="dashboard-band trend-band">
          <header className="dashboard-band-head">
            <h2>
              沟通趋势 <small>近7天</small>
            </h2>
          </header>
          <div className="chart-wrap">
            {data &&
            data.trend.every(
              (p) => p.communication !== null && p.effective !== null,
            ) ? (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart
                  data={data.trend}
                  margin={{ top: 10, right: 24, left: -17, bottom: 0 }}
                >
                  <CartesianGrid stroke="#ebedf2" vertical={false} />
                  <XAxis
                    dataKey="date"
                    tickFormatter={(v) => String(v).slice(5)}
                    tickLine={false}
                    axisLine={false}
                    tick={{ fontSize: 12, fill: "#7a8393" }}
                  />
                  <YAxis
                    allowDecimals={false}
                    tickLine={false}
                    axisLine={false}
                    tick={{ fontSize: 12, fill: "#7a8393" }}
                  />
                  <Tooltip />
                  <Legend
                    verticalAlign="top"
                    align="right"
                    height={0}
                    iconSize={8}
                    wrapperStyle={{ fontSize: 12, top: -36, right: 24 }}
                  />
                  <Area
                    name="沟通客户"
                    dataKey="communication"
                    type="linear"
                    stroke="#7d8795"
                    fill="#f3f4f6"
                    strokeWidth={2}
                    isAnimationActive={false}
                  />
                  <Area
                    name="有效沟通"
                    dataKey="effective"
                    type="linear"
                    stroke="#3878ed"
                    fill="#e8effc"
                    strokeWidth={2}
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <div className="state-box">
                {loading ? "正在加载趋势" : "趋势数据暂不可用"}
              </div>
            )}
          </div>
        </section>
        <section className="dashboard-band vehicles-band">
          <header className="dashboard-band-head">
            <h2>车辆经营 · 整体</h2>
            <div className="vehicle-period">
              <select
                aria-label="车辆已售统计日期"
                value={vehiclePeriod}
                onChange={(e) => setVehiclePeriod(e.target.value as Period)}
              >
                {periods.map((v) => (
                  <option key={v}>{v}</option>
                ))}
              </select>
            </div>
          </header>
          <Dates
            period={vehiclePeriod}
            value={vehicleDates}
            onChange={setVehicleDates}
            label="车辆"
            today={today}
          />
          <div className="inventory-current">
            <CarFront size={19} />
            <span>当前在售</span>
            <strong>
              {formatMetric(data?.vehicles.listed, "listed")}
              <small> 台</small>
            </strong>
            <span className="current-tag">实时</span>
          </div>
          <div className="vehicle-kpi">
            <Archive size={19} />
            <span>已售库存</span>
            <strong title={data?.vehicles.sold.reason || undefined}>
              {formatMetric(data?.vehicles.sold, "sold")}
              <small> 台</small>
            </strong>
          </div>
          <div className="vehicle-kpi">
            <Banknote size={19} />
            <span>成交金额</span>
            <strong
              className="amount"
              title={data?.vehicles.amount.reason || undefined}
            >
              {formatMetric(data?.vehicles.amount, "amount")}
            </strong>
          </div>

        </section>
      </div>
      <section className="dashboard-band dashboard-sales">
        <header className="dashboard-band-head">
          <h2>销售明细</h2>
          <span>
            {period} · {mode === "all" ? "全部" : mode === "ai" ? "AI" : "人工"}
            沟通
          </span>
        </header>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>销售</th>
                {metrics.map((m) => (
                  <th key={m.key}>{m.name}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data?.sales.map((row) => (
                <tr key={row.sales_id}>
                  <td>
                    <span className="small-avatar">{row.name[0]}</span>
                    {row.name}
                    {!row.enabled && "（已停用）"}
                  </td>
                  {metrics.map((m) => (
                    <td
                      key={m.key}
                      title={row.metrics[m.key].reason || undefined}
                    >
                      {formatMetric(row.metrics[m.key], m.key)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {!loading && !error && data?.sales.length === 0 && (
            <div className="state-box">暂无符合条件的数据</div>
          )}
        </div>
      </section>
    </div>
  );
}
