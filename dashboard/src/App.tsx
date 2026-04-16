import { LiquidOcean } from "@/components/ui/liquid-ocean";
import { useEffect, useState, useRef } from "react";
import { Sun, Moon, Settings, MessageCircle, TrendingUp, TrendingDown, Check, ArrowUpRight } from "lucide-react";

// ─── Shared helpers ────────────────────────────────────────────────────────────

function useParallax(strength = 12) {
  const [pos, setPos] = useState({ x: 0, y: 0 });
  useEffect(() => {
    const move = (e: MouseEvent) => {
      const cx = window.innerWidth / 2, cy = window.innerHeight / 2;
      setPos({ x: (e.clientX - cx) / cx, y: (e.clientY - cy) / cy });
    };
    window.addEventListener("mousemove", move);
    return () => window.removeEventListener("mousemove", move);
  }, []);
  return { x: pos.x * strength, y: pos.y * strength };
}

// ─── Widget Shell ────────────────────────────────────────────────────────────

interface WidgetProps {
  children: React.ReactNode;
  className?: string;
  label?: string;
  icon?: React.ReactNode;
  accent?: boolean;
  delay?: number;
}

function Widget({ children, className = "", label, icon, accent = false, delay = 0 }: WidgetProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [hovered, setHovered] = useState(false);
  const [tilt, setTilt] = useState({ x: 0, y: 0 });

  const handleMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!ref.current) return;
    const rect = ref.current.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width - 0.5) * 8;
    const y = ((e.clientY - rect.top) / rect.height - 0.5) * 8;
    setTilt({ x: -y, y: x });
  };

  return (
    <div
      ref={ref}
      className={`widget-card group relative overflow-hidden ${className}`}
      style={{ animationDelay: `${delay}ms`, transform: hovered ? `perspective(1000px) rotateX(${tilt.x}deg) rotateY(${tilt.y}deg) translateZ(4px)` : "perspective(1000px) rotateX(0) rotateY(0)" }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => { setHovered(false); setTilt({ x: 0, y: 0 }); }}
      onMouseMove={handleMouseMove}
    >
      {/* Background layers */}
      <div className="absolute inset-0 rounded-[20px] bg-white/70 dark:bg-[#120808]/80 backdrop-blur-2xl" />
      <div className="absolute inset-0 rounded-[20px] border border-black/[0.06] dark:border-white/[0.06]" />

      {/* Hover glow */}
      <div
        className="absolute inset-0 rounded-[20px] opacity-0 group-hover:opacity-100 transition-opacity duration-500 pointer-events-none"
        style={{ background: accent ? "radial-gradient(circle at 50% 0%, rgba(255,17,0,0.08), transparent 70%)" : "radial-gradient(circle at 50% 0%, rgba(255,255,255,0.06), transparent 70%)" }}
      />

      {/* Top edge light */}
      <div className="absolute inset-x-0 top-0 h-px rounded-t-[20px] bg-gradient-to-r from-transparent via-white/40 dark:via-white/10 to-transparent" />

      {/* Shadow */}
      <div
        className="absolute inset-0 rounded-[20px] -z-10 transition-all duration-500"
        style={{ boxShadow: hovered ? "0 20px 60px rgba(0,0,0,0.14), 0 4px 12px rgba(0,0,0,0.06)" : "0 4px 24px rgba(0,0,0,0.05), 0 1px 4px rgba(0,0,0,0.04)" }}
      />

      {/* Content */}
      <div className="relative z-10 p-5 h-full flex flex-col">
        {(label || icon) && (
          <div className="flex items-center gap-1.5 mb-3">
            {icon && <span className="text-red-500/80 w-3 h-3">{icon}</span>}
            {label && (
              <span className="text-[10px] font-mono uppercase tracking-[0.18em] text-neutral-400 dark:text-neutral-500 font-medium">
                {label}
              </span>
            )}
          </div>
        )}
        <div className="flex-1 min-h-0">{children}</div>
      </div>
    </div>
  );
}

// ─── Time Widget ────────────────────────────────────────────────────────────

function TimeWidget() {
  const [time, setTime] = useState({ h: "", m: "", s: "", period: "", date: "" });

  useEffect(() => {
    const update = () => {
      const now = new Date();
      const h = now.toLocaleTimeString("en-US", { hour: "2-digit", hour12: true }).split(":")[0];
      const m = String(now.getMinutes()).padStart(2, "0");
      const s = String(now.getSeconds()).padStart(2, "0");
      const period = now.getHours() >= 12 ? "PM" : "AM";
      const date = now.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
      setTime({ h, m, s, period, date });
    };
    update();
    const id = setInterval(update, 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="flex flex-col justify-between h-full py-1">
      <div className="flex items-end gap-0 leading-none">
        <span
          className="font-bold tracking-[-0.04em] text-neutral-900 dark:text-white select-none"
          style={{ fontSize: "clamp(48px, 8vw, 72px)", lineHeight: 1, fontVariantNumeric: "tabular-nums" }}
        >
          {time.h}:{time.m}
        </span>
        <div className="flex flex-col ml-2 mb-1 gap-1">
          <span className="text-xs font-mono font-semibold text-red-500 tracking-wider">{time.period}</span>
          <span className="text-[10px] font-mono text-neutral-400 dark:text-neutral-500 tabular-nums">{time.s}s</span>
        </div>
      </div>
      <div>
        <div className="text-sm font-medium text-neutral-500 dark:text-neutral-400 mt-2 tracking-tight">
          {time.date}
        </div>
        <div className="mt-2 flex items-center gap-1.5">
          <span className="inline-block w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" />
          <span className="text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-400 dark:text-neutral-500">
            System Online
          </span>
        </div>
      </div>
    </div>
  );
}

// ─── Weather Widget ────────────────────────────────────────────────────────────

const WEATHER_ICONS: Record<string, string> = {
  Sunny: "☀️", "Partly Cloudy": "⛅", Clear: "🌤", Warm: "🌡️",
};

function WeatherWidget() {
  const [loading, setLoading] = useState(true);
  const [data, setData] = useState({ temp: 28, desc: "Partly Cloudy", humidity: 65, feels: 31 });

  useEffect(() => {
    const t = setTimeout(() => {
      setData({
        temp: 28 + Math.floor(Math.random() * 5),
        desc: (["Sunny", "Partly Cloudy", "Clear", "Warm"] as const)[Math.floor(Math.random() * 4)],
        humidity: 60 + Math.floor(Math.random() * 20),
        feels: 30 + Math.floor(Math.random() * 4),
      });
      setLoading(false);
    }, 900);
    return () => clearTimeout(t);
  }, []);

  return (
    <div className="flex flex-col justify-between h-full">
      {loading ? (
        <div className="space-y-3 animate-pulse">
          <div className="h-10 w-24 bg-neutral-200 dark:bg-neutral-800 rounded-xl" />
          <div className="h-3 w-28 bg-neutral-100 dark:bg-neutral-800/60 rounded" />
          <div className="h-3 w-20 bg-neutral-100 dark:bg-neutral-800/60 rounded" />
        </div>
      ) : (
        <>
          <div>
            <div className="flex items-start justify-between">
              <span className="text-4xl font-bold tracking-tight text-neutral-900 dark:text-white tabular-nums">
                {data.temp}°
              </span>
              <span className="text-2xl mt-0.5">{WEATHER_ICONS[data.desc] ?? "🌡️"}</span>
            </div>
            <div className="mt-1 text-sm font-medium text-neutral-600 dark:text-neutral-300">
              {data.desc}
            </div>
            <div className="text-xs text-neutral-400 dark:text-neutral-500 font-mono mt-0.5">
              Bengaluru, IN
            </div>
          </div>
          <div className="flex gap-2 mt-3">
            <div className="flex-1 rounded-xl bg-neutral-100/80 dark:bg-white/[0.04] border border-black/[0.04] dark:border-white/[0.04] p-2.5">
              <div className="text-[9px] font-mono uppercase tracking-wider text-neutral-400 mb-0.5">Feels</div>
              <div className="text-sm font-semibold text-neutral-700 dark:text-neutral-200 tabular-nums">{data.feels}°C</div>
            </div>
            <div className="flex-1 rounded-xl bg-neutral-100/80 dark:bg-white/[0.04] border border-black/[0.04] dark:border-white/[0.04] p-2.5">
              <div className="text-[9px] font-mono uppercase tracking-wider text-neutral-400 mb-0.5">Humid</div>
              <div className="text-sm font-semibold text-neutral-700 dark:text-neutral-200 tabular-nums">{data.humidity}%</div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

// ─── Markets Widget ────────────────────────────────────────────────────────────

const STOCKS = [
  { symbol: "NIFTY", value: "24,582", change: +0.82 },
  { symbol: "SENSEX", value: "80,456", change: +0.65 },
  { symbol: "AAPL", value: "228.45", change: -0.34 },
];

function MarketsWidget() {
  return (
    <div className="flex flex-col justify-center h-full gap-2">
      {STOCKS.map((s) => {
        const up = s.change >= 0;
        return (
          <div
            key={s.symbol}
            className="group/row flex items-center justify-between px-3 py-2.5 rounded-[12px] bg-neutral-50/80 dark:bg-white/[0.03] border border-black/[0.04] dark:border-white/[0.04] hover:border-red-500/20 hover:bg-neutral-100/80 dark:hover:bg-white/[0.06] transition-all duration-200 cursor-default"
          >
            <span className="text-xs font-semibold text-neutral-600 dark:text-neutral-300 font-mono tracking-wider">
              {s.symbol}
            </span>
            <div className="flex items-center gap-2.5">
              <span className="text-xs font-mono font-medium text-neutral-700 dark:text-neutral-200 tabular-nums">
                {s.value}
              </span>
              <span
                className={`flex items-center gap-0.5 text-[10px] font-bold px-2 py-0.5 rounded-full tabular-nums ${
                  up
                    ? "bg-green-100 text-green-700 dark:bg-green-500/10 dark:text-green-400"
                    : "bg-red-100 text-red-600 dark:bg-red-500/10 dark:text-red-400"
                }`}
              >
                {up ? <TrendingUp className="w-2.5 h-2.5" /> : <TrendingDown className="w-2.5 h-2.5" />}
                {up ? "+" : ""}{s.change}%
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ─── Tasks Widget ────────────────────────────────────────────────────────────

const INITIAL_TASKS = [
  { id: 1, text: "Review quarterly goals", done: false, priority: "high" },
  { id: 2, text: "Team standup at 3 PM", done: true, priority: "medium" },
  { id: 3, text: "Send investor update", done: false, priority: "high" },
];

function TasksWidget() {
  const [tasks, setTasks] = useState(INITIAL_TASKS);

  const toggle = (id: number) =>
    setTasks((prev) => prev.map((t) => (t.id === id ? { ...t, done: !t.done } : t)));

  const remaining = tasks.filter((t) => !t.done).length;

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold tabular-nums text-neutral-900 dark:text-white">{remaining}</span>
          <span className="text-xs text-neutral-400 dark:text-neutral-500 font-mono">remaining</span>
        </div>
        <div className="flex gap-1">
          {tasks.map((t) => (
            <div
              key={t.id}
              className={`w-1.5 h-1.5 rounded-full transition-all duration-300 ${t.done ? "bg-green-400" : "bg-neutral-300 dark:bg-neutral-600"}`}
            />
          ))}
        </div>
      </div>
      <div className="flex flex-col gap-1.5 flex-1">
        {tasks.map((task) => (
          <button
            key={task.id}
            onClick={() => toggle(task.id)}
            className={`group/task flex items-center gap-3 px-3 py-2.5 rounded-[10px] text-left transition-all duration-300 w-full border ${
              task.done
                ? "opacity-40 bg-transparent border-transparent"
                : "bg-neutral-50/80 dark:bg-white/[0.03] border-black/[0.04] dark:border-white/[0.04] hover:border-red-500/20 hover:bg-red-50/30 dark:hover:bg-red-500/[0.04]"
            }`}
          >
            <div
              className={`w-4 h-4 rounded-full border-[1.5px] flex items-center justify-center flex-shrink-0 transition-all duration-300 ${
                task.done
                  ? "bg-green-500 border-green-500"
                  : "border-neutral-300 dark:border-neutral-600 group-hover/task:border-red-400"
              }`}
            >
              {task.done && <Check className="w-2.5 h-2.5 text-white" strokeWidth={3} />}
            </div>
            <span
              className={`text-sm leading-snug transition-all duration-300 ${
                task.done
                  ? "line-through text-neutral-400 dark:text-neutral-600"
                  : "text-neutral-700 dark:text-neutral-200 font-medium"
              }`}
            >
              {task.text}
            </span>
            {!task.done && task.priority === "high" && (
              <span className="ml-auto text-[9px] font-mono uppercase tracking-wider text-red-400/70 flex-shrink-0">
                urgent
              </span>
            )}
          </button>
        ))}
      </div>
    </div>
  );
}

// ─── Quick Actions Row ────────────────────────────────────────────────────────

const QUICK_LINKS = [
  { label: "Open Chat", href: "/chat-ui", icon: <MessageCircle className="w-3.5 h-3.5" /> },
  { label: "Settings", href: "#", icon: <Settings className="w-3.5 h-3.5" /> },
];

function QuickActions() {
  return (
    <div className="flex gap-3 justify-end items-center">
      {QUICK_LINKS.map((l) => (
        <a
          key={l.label}
          href={l.href}
          className="flex items-center gap-2 px-4 py-2 rounded-[10px] bg-neutral-100/80 dark:bg-white/[0.04] border border-black/[0.05] dark:border-white/[0.06] text-xs font-semibold text-neutral-600 dark:text-neutral-300 hover:text-neutral-900 dark:hover:text-white hover:bg-neutral-200/80 dark:hover:bg-white/[0.08] hover:border-neutral-300/60 dark:hover:border-white/[0.1] transition-all duration-200 hover:-translate-y-px active:translate-y-0 group"
        >
          <span className="text-neutral-400 dark:text-neutral-500 group-hover:text-red-500 transition-colors duration-200">
            {l.icon}
          </span>
          {l.label}
          <ArrowUpRight className="w-3 h-3 opacity-0 group-hover:opacity-50 transition-opacity duration-200 -ml-0.5" />
        </a>
      ))}
    </div>
  );
}

// ─── Navigation ────────────────────────────────────────────────────────────

function Nav({ isDark, onToggle }: { isDark: boolean; onToggle: () => void }) {
  return (
    <nav className="flex items-center justify-between mb-10">
      {/* Brand */}
      <div className="flex items-center gap-3.5">
        <div className="relative">
          <div className="w-10 h-10 rounded-[13px] bg-gradient-to-br from-red-500 to-red-700 flex items-center justify-center shadow-lg shadow-red-500/30 hover:shadow-red-500/50 transition-all duration-300 hover:scale-105 cursor-pointer group">
            <svg viewBox="0 0 24 24" fill="none" className="w-5 h-5" xmlns="http://www.w3.org/2000/svg">
              <path d="M12 2L3 22H7L12 10L17 22H21L12 2Z" fill="white" />
              <path d="M9 16H15" stroke="white" strokeWidth="2.5" strokeLinecap="round" />
            </svg>
            {/* Ambient glow */}
            <div className="absolute inset-0 rounded-[13px] bg-red-400/30 blur-md -z-10 opacity-0 group-hover:opacity-100 transition-opacity duration-300" />
          </div>
          {/* Online indicator */}
          <span className="absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-green-400 border-2 border-white dark:border-[#0a0404]" />
        </div>
        <div>
          <div className="text-[15px] font-bold tracking-tight text-neutral-900 dark:text-white leading-none">
            Alfred
          </div>
          <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-neutral-400 dark:text-neutral-500 mt-0.5">
            Command Center
          </div>
        </div>
      </div>

      {/* Controls */}
      <div className="flex items-center gap-1.5">
        <button
          onClick={onToggle}
          className="w-9 h-9 rounded-[10px] flex items-center justify-center text-neutral-500 dark:text-neutral-400 hover:text-neutral-900 dark:hover:text-white hover:bg-neutral-100 dark:hover:bg-white/[0.06] border border-transparent hover:border-black/[0.06] dark:hover:border-white/[0.06] transition-all duration-200 hover:scale-105 active:scale-95"
          title="Toggle theme"
        >
          {isDark
            ? <Sun className="w-4 h-4" />
            : <Moon className="w-4 h-4" />
          }
        </button>
        <a
          href="/chat-ui"
          className="flex items-center gap-2 px-4 py-2 rounded-[10px] bg-neutral-900 dark:bg-white text-white dark:text-neutral-900 text-xs font-bold tracking-wide hover:bg-red-600 dark:hover:bg-red-500 dark:hover:text-white transition-all duration-300 hover:scale-[1.03] hover:shadow-lg hover:shadow-red-500/20 active:scale-95"
        >
          <MessageCircle className="w-3.5 h-3.5" />
          Chat
        </a>
      </div>
    </nav>
  );
}

// ─── Main App ────────────────────────────────────────────────────────────

export default function App() {
  const [isDark, setIsDark] = useState(false);
  const [mounted, setMounted] = useState(false);
  const parallax = useParallax(6);

  useEffect(() => {
    const saved = localStorage.getItem("alfred-theme") || "light";
    const dark = saved === "dark";
    setIsDark(dark);
    document.documentElement.setAttribute("data-theme", saved);
    setMounted(true);
  }, []);

  const toggleTheme = () => {
    const next = !isDark;
    setIsDark(next);
    const val = next ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", val);
    localStorage.setItem("alfred-theme", val);
  };

  return (
    <div className="min-h-screen bg-[#fdf5f5] dark:bg-[#0a0404] transition-colors duration-500 overflow-hidden">

      {/* 3D Background */}
      <div className="fixed inset-0 z-0">
        <LiquidOcean backgroundColor={0x0a0404} accentColor={0xff1100} boatCount={3} />
      </div>

      {/* Ambient radial overlays */}
      <div
        className="fixed inset-0 z-0 pointer-events-none"
        style={{
          background: "radial-gradient(ellipse 60% 40% at 80% 20%, rgba(255,17,0,0.06), transparent), radial-gradient(ellipse 40% 30% at 20% 80%, rgba(255,17,0,0.04), transparent)",
        }}
      />

      {/* Subtle grid texture */}
      <div
        className="fixed inset-0 z-0 pointer-events-none opacity-[0.025] dark:opacity-[0.015]"
        style={{
          backgroundImage: "linear-gradient(rgba(0,0,0,0.8) 1px, transparent 1px), linear-gradient(90deg, rgba(0,0,0,0.8) 1px, transparent 1px)",
          backgroundSize: "80px 80px",
        }}
      />

      {/* Content layer */}
      <div
        className={`relative z-10 min-h-screen transition-opacity duration-700 ${mounted ? "opacity-100" : "opacity-0"}`}
        style={{ transform: `translate(${-parallax.x * 0.3}px, ${-parallax.y * 0.3}px)` }}
      >
        <div className="max-w-5xl mx-auto px-6 py-10 md:px-10 md:py-12">

          <Nav isDark={isDark} onToggle={toggleTheme} />

          {/* Page heading */}
          <div className="mb-8 dashboard-heading">
            <h2 className="text-2xl font-bold tracking-tight text-neutral-900 dark:text-white leading-none">
              Good {new Date().getHours() < 12 ? "morning" : new Date().getHours() < 17 ? "afternoon" : "evening"}.
            </h2>
            <p className="text-sm text-neutral-400 dark:text-neutral-500 mt-1 font-mono">
              Here's your overview for today.
            </p>
          </div>

          {/* Widget grid — asymmetric layout */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3 md:gap-4">

            {/* Time — spans 2 columns */}
            <Widget
              label="Local Time"
              icon={<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="6" cy="6" r="5"/><path d="M6 3v3l2 1.5" strokeLinecap="round"/></svg>}
              className="lg:col-span-2 row-span-1 widget-enter"
              delay={0}
              accent
            >
              <TimeWidget />
            </Widget>

            {/* Weather */}
            <Widget
              label="Weather · Bengaluru"
              icon={<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="6" cy="5" r="2.5"/><path d="M2 9a2 2 0 012-2h4a2 2 0 110 4H4a2 2 0 01-2-2z"/></svg>}
              className="widget-enter"
              delay={80}
            >
              <WeatherWidget />
            </Widget>

            {/* Markets */}
            <Widget
              label="Markets"
              icon={<TrendingUp className="w-3 h-3" />}
              className="widget-enter"
              delay={160}
            >
              <MarketsWidget />
            </Widget>

            {/* Tasks — full width */}
            <Widget
              label="Today's Tasks"
              icon={<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5"><rect x="1" y="1" width="10" height="10" rx="2"/><path d="M4 6l1.5 1.5L8 4" strokeLinecap="round" strokeLinejoin="round"/></svg>}
              className="lg:col-span-4 widget-enter"
              delay={240}
            >
              <TasksWidget />
            </Widget>

          </div>

          {/* Footer bar */}
          <div className="mt-6 flex items-center justify-between">
            <p className="text-[11px] font-mono text-neutral-300 dark:text-neutral-700 tracking-wider uppercase">
              Alfred — Personal Intelligence
            </p>
            <QuickActions />
          </div>

        </div>
      </div>
    </div>
  );
}
