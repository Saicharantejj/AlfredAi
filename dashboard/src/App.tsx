import { LiquidOcean } from "@/components/ui/liquid-ocean";
import { useEffect, useState } from "react";
import { Sun, Moon, Settings, MessageCircle } from "lucide-react";

// Widget Card Component
interface WidgetProps {
  children: React.ReactNode;
  className?: string;
  icon?: React.ReactNode;
  label?: string;
}

function Widget({ children, className = "", icon, label }: WidgetProps) {
  return (
    <div
      className={`
        relative overflow-hidden rounded-2xl border
        bg-white/70 dark:bg-neutral-900/70
        backdrop-blur-xl
        shadow-[0_8px_32px_rgba(0,0,0,0.06)]
        dark:shadow-[0_8px_32px_rgba(0,0,0,0.4)]
        transition-all duration-500 var(--ease-out)
        hover:shadow-[0_16px_48px_rgba(0,0,0,0.12)]
        dark:hover:shadow-[0_16px_48px_rgba(0,0,0,0.6)]
        hover:scale-[1.02] hover:border-red-500/40
        hover:bg-white/80 dark:hover:bg-neutral-900/80
        group
        ${className}
      `}
      style={{ transitionTimingFunction: 'var(--ease-spring)' }}
    >
      {/* Subtle shine effect on hover */}
      <div className="absolute inset-0 bg-gradient-to-br from-white/0 via-white/50 to-white/0 opacity-0 group-hover:opacity-100 transition-opacity duration-700 pointer-events-none" />

      {/* Red glow on hover */}
      <div className="absolute -inset-1 bg-gradient-to-r from-red-500/0 via-red-500/10 to-red-500/0 opacity-0 group-hover:opacity-100 blur transition-opacity duration-500 pointer-events-none" />

      {/* Content */}
      <div className="relative p-5 h-full flex flex-col">
        {(icon || label) && (
          <div className="flex items-center gap-2 mb-3 text-xs font-mono uppercase tracking-wider text-neutral-500 dark:text-neutral-400">
            {icon && <span className="text-red-500">{icon}</span>}
            {label && <span>{label}</span>}
          </div>
        )}
        <div className="flex-1">{children}</div>
      </div>
    </div>
  );
}

// Time Widget
function TimeWidget() {
  const [time, setTime] = useState("");
  const [date, setDate] = useState("");

  useEffect(() => {
    const update = () => {
      const now = new Date();
      setTime(now.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: true }));
      setDate(now.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" }));
    };
    update();
    const interval = setInterval(update, 1000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="flex flex-col justify-center h-full">
      <div className="text-5xl font-bold tracking-tighter bg-gradient-to-br from-neutral-900 to-neutral-600 dark:from-white dark:to-neutral-400 bg-clip-text text-transparent">
        {time}
      </div>
      <div className="text-sm text-neutral-500 dark:text-neutral-400 font-medium mt-1">
        {date}
      </div>
    </div>
  );
}

// Weather Widget
function WeatherWidget() {
  const [loading, setLoading] = useState(true);
  const [data, setData] = useState({ temp: 28, desc: "Partly Cloudy", humidity: 65, feels: 31 });

  useEffect(() => {
    // Simulate API call
    const timer = setTimeout(() => {
      setData({
        temp: 28 + Math.floor(Math.random() * 5),
        desc: ["Sunny", "Partly Cloudy", "Clear", "Warm"][Math.floor(Math.random() * 4)],
        humidity: 60 + Math.floor(Math.random() * 20),
        feels: 30 + Math.floor(Math.random() * 4),
      });
      setLoading(false);
    }, 800);
    return () => clearTimeout(timer);
  }, []);

  return (
    <div className="flex flex-col justify-center h-full gap-3">
      {loading ? (
        <div className="animate-pulse space-y-2">
          <div className="h-10 w-20 bg-neutral-200 dark:bg-neutral-800 rounded-lg" />
          <div className="h-4 w-32 bg-neutral-200 dark:bg-neutral-800 rounded" />
        </div>
      ) : (
        <>
          <div className="text-4xl font-bold tracking-tight">
            {data.temp}°C
          </div>
          <div className="text-sm text-neutral-600 dark:text-neutral-300 font-medium">
            {data.desc} · Bengaluru
          </div>
          <div className="flex gap-2">
            <span className="text-xs px-2.5 py-1 rounded-full bg-neutral-100 dark:bg-neutral-800 text-neutral-600 dark:text-neutral-300 font-medium">
              Feels {data.feels}°
            </span>
            <span className="text-xs px-2.5 py-1 rounded-full bg-neutral-100 dark:bg-neutral-800 text-neutral-600 dark:text-neutral-300 font-medium">
              {data.humidity}% humid
            </span>
          </div>
        </>
      )}
    </div>
  );
}

// Markets Widget
function MarketsWidget() {
  const stocks = [
    { symbol: "NIFTY", value: "24,582", change: +0.82 },
    { symbol: "SENSEX", value: "80,456", change: +0.65 },
    { symbol: "AAPL", value: "228.45", change: -0.34 },
  ];

  return (
    <div className="flex flex-col justify-center h-full gap-2.5">
      {stocks.map((stock) => (
        <div
          key={stock.symbol}
          className="flex items-center justify-between group/stock cursor-default"
        >
          <span className="text-sm font-semibold text-neutral-700 dark:text-neutral-200 group-hover/stock:text-red-500 transition-colors">
            {stock.symbol}
          </span>
          <div className="flex items-center gap-3">
            <span className="text-sm font-mono text-neutral-600 dark:text-neutral-300">
              {stock.value}
            </span>
            <span
              className={`text-xs font-bold px-1.5 py-0.5 rounded ${
                stock.change >= 0
                  ? "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400"
                  : "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400"
              }`}
            >
              {stock.change >= 0 ? "+" : ""}{stock.change}%
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

// Tasks Widget
function TasksWidget() {
  const [tasks] = useState([
    { text: "Review quarterly goals", done: false },
    { text: "Team standup at 3 PM", done: true },
    { text: "Send investor update", done: false },
  ]);

  return (
    <div className="flex flex-col h-full gap-2 overflow-hidden">
      {tasks.map((task, i) => (
        <div
          key={i}
          className={`
            flex items-center gap-3 p-2 rounded-lg
            transition-all duration-300
            ${task.done ? "opacity-50" : "opacity-100"}
            hover:bg-neutral-100 dark:hover:bg-neutral-800/50
          `}
        >
          <div
            className={`
              w-4 h-4 rounded-full border-2 flex items-center justify-center
              transition-all duration-300
              ${task.done
                ? "bg-green-500 border-green-500"
                : "border-neutral-300 dark:border-neutral-600 hover:border-red-400"
              }
            `}
          >
            {task.done && (
              <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
              </svg>
            )}
          </div>
          <span className={`text-sm ${task.done ? "line-through text-neutral-400" : "text-neutral-700 dark:text-neutral-200"}`}>
            {task.text}
          </span>
        </div>
      ))}
    </div>
  );
}

// Command Bar Component
function CommandBar() {
  const [isDark, setIsDark] = useState(false);

  useEffect(() => {
    const isDarkMode = document.documentElement.getAttribute("data-theme") === "dark";
    setIsDark(isDarkMode);
  }, []);

  const toggleTheme = () => {
    const newDark = !isDark;
    setIsDark(newDark);
    document.documentElement.setAttribute("data-theme", newDark ? "dark" : "light");
  };

  return (
    <div className="w-full">
      {/* Top Navigation */}
      <nav className="flex items-center justify-between mb-8 pb-6 border-b border-neutral-200/50 dark:border-neutral-800/50">
        <div className="flex items-center gap-4">
          <div className="w-11 h-11 rounded-lg bg-gradient-to-br from-red-500 to-red-600 flex items-center justify-center shadow-lg shadow-red-500/25 transition-transform duration-300 hover:scale-105 cursor-pointer">
            <span className="text-white font-black text-lg">A</span>
          </div>
          <div>
            <h1 className="text-xl font-bold tracking-tight">Alfred</h1>
            <p className="text-[10px] font-mono uppercase tracking-[0.2em] text-neutral-500">Command Center</p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={toggleTheme}
            className="p-2.5 rounded-xl hover:bg-neutral-100 dark:hover:bg-neutral-800 transition-all duration-300 hover:scale-110 hover:rotate-12 active:scale-95"
            title="Toggle theme"
          >
            {isDark ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
          </button>
          <button className="p-2.5 rounded-xl hover:bg-neutral-100 dark:hover:bg-neutral-800 transition-all duration-300 hover:scale-110 hover:rotate-45 active:scale-95">
            <Settings className="w-4 h-4" />
          </button>
          <a
            href="/chat-ui"
            className="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-gradient-to-r from-neutral-900 to-neutral-800 dark:from-white dark:to-neutral-100 text-white dark:text-neutral-900 text-sm font-semibold transition-all duration-300 hover:scale-105 hover:shadow-xl hover:shadow-neutral-900/20 dark:hover:shadow-white/20 active:scale-95"
          >
            <MessageCircle className="w-4 h-4" />
            <span>Chat</span>
          </a>
        </div>
      </nav>

      {/* Widget Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <Widget icon="●" label="Local Time" className="md:col-span-1 widget-enter animate-fade-in-up">
          <TimeWidget />
        </Widget>

        <Widget icon="●" label="Weather · Bengaluru" className="md:col-span-1 widget-enter animate-fade-in-up">
          <WeatherWidget />
        </Widget>

        <Widget icon="●" label="Markets" className="md:col-span-1 widget-enter animate-fade-in-up">
          <MarketsWidget />
        </Widget>

        <Widget icon="●" label="Tasks" className="md:col-span-1 widget-enter animate-fade-in-up">
          <TasksWidget />
        </Widget>
      </div>
    </div>
  );
}

// Main App
export default function App() {
  return (
    <div className="min-h-screen bg-[#fdf5f5] dark:bg-[#0a0505] transition-colors duration-500">
      {/* Animated Background */}
      <div className="fixed inset-0 z-0">
        <LiquidOcean
          backgroundColor={0x0a0505}
          accentColor={0xff1100}
          boatCount={3}
        />
      </div>

      {/* Content */}
      <div className="relative z-10 min-h-screen">
        <div className="max-w-7xl mx-auto px-8 py-12">
          <CommandBar />

          {/* Footer */}
          <footer className="mt-16 text-center text-sm text-neutral-400 dark:text-neutral-600 font-mono">
            Alfred Dashboard — Your Personal Command Center
          </footer>
        </div>
      </div>
    </div>
  );
}
