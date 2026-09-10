import { useState } from "react"

type ConnectionState = "idle" | "connected" | "disconnected"

interface Device {
  id: string
  name: string
  ip: string
  connectionState: ConnectionState
  cleanActive: boolean
  autoOn: boolean
  temperature: number
}

const TEMP_ALERT = 70

const seed: Device[] = [
  { id: "1", name: "Camera 1", ip: "192.168.1.101", connectionState: "idle", cleanActive: false, autoOn: false, temperature: 42 },
  { id: "2", name: "Camera 2", ip: "192.168.1.102", connectionState: "connected", cleanActive: true, autoOn: true, temperature: 78 },
  { id: "3", name: "Camera 3", ip: "192.168.1.103", connectionState: "disconnected", cleanActive: false, autoOn: false, temperature: 65 },
]

let _id = 4

/* ─── Separator ──────────────────────────────────────────────────── */
function VSep() {
  return <div className="w-px self-stretch mx-1 bg-[#1f2330] shrink-0" />
}

/* ─── Section wrapper ─────────────────────────────────────────────── */
function Section({ label, children, width }: { label: string; children: React.ReactNode; width?: string }) {
  return (
    <div className={`flex flex-col gap-2.5 shrink-0 ${width ?? ""}`}>
      <span className="text-[9.5px] font-semibold tracking-[0.14em] uppercase text-[#454c5a]">
        {label}
      </span>
      {children}
    </div>
  )
}

/* ─── DeviceCard ──────────────────────────────────────────────────── */
function DeviceCard({ device, onUpdate }: { device: Device; onUpdate: (d: Device) => void }) {
  const tempAlert = device.temperature >= TEMP_ALERT

  const connCfg = {
    idle: {
      label: "Connect",
      bg: "bg-blue-600 hover:bg-blue-500 ring-blue-700/40",
      dot: "bg-blue-300 status-pulse",
      text: "text-white",
    },
    connected: {
      label: "Connected",
      bg: "bg-emerald-700 hover:bg-emerald-600 ring-emerald-700/40",
      dot: "bg-emerald-300 status-pulse",
      text: "text-white",
    },
    disconnected: {
      label: "Reconnect",
      bg: "bg-red-700 hover:bg-red-600 ring-red-700/40",
      dot: "bg-red-300",
      text: "text-white",
    },
  }[device.connectionState]

  function cycleConn() {
    const next: ConnectionState =
      device.connectionState === "idle" ? "connected"
      : device.connectionState === "connected" ? "disconnected"
      : "idle"
    onUpdate({ ...device, connectionState: next })
  }

  return (
    <div className="flex items-center gap-0 rounded-xl border border-[#1e2230] bg-[#181c25] shadow-[0_2px_16px_rgba(0,0,0,0.35)] px-5 py-0 min-h-[142px] transition-colors hover:border-[#272c3a]">

      {/* ① Device Name */}
      <Section label="Device Name" width="w-[188px]">
        <div className="relative">
          <input
            value={device.name}
            onChange={e => onUpdate({ ...device, name: e.target.value })}
            spellCheck={false}
            className="w-full bg-[#111419] border border-[#252932] rounded-lg pl-3 pr-3 py-2 text-[13px] text-[#d8e0ed] placeholder-[#343a47] focus:outline-none focus:border-blue-500/50 focus:ring-1 focus:ring-blue-500/20 transition-all"
            placeholder="Camera name…"
          />
          <div className="absolute right-2.5 top-1/2 -translate-y-1/2">
            <svg width="11" height="11" viewBox="0 0 11 11" fill="none" className="text-[#343a47]">
              <path d="M1 9.5L7.5 3 9 4.5l-6.5 6.5H1V9.5z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
              <path d="M6.5 2L9 4.5" stroke="currentColor" strokeWidth="1.2" />
            </svg>
          </div>
        </div>
      </Section>

      <VSep />

      {/* ② IP Address */}
      <Section label="IP Address" width="w-[176px]">
        <div className="px-4">
          <input
            value={device.ip}
            onChange={e => onUpdate({ ...device, ip: e.target.value })}
            spellCheck={false}
            className="w-full bg-[#111419] border border-[#252932] rounded-lg px-3 py-2 text-[13px] font-mono text-[#9bacc4] placeholder-[#343a47] focus:outline-none focus:border-blue-500/50 focus:ring-1 focus:ring-blue-500/20 transition-all tracking-wider"
            placeholder="192.168.x.x"
          />
        </div>
      </Section>

      <VSep />

      {/* ③ Connection */}
      <Section label="Connection" width="w-[162px]">
        <div className="px-4">
          <button
            onClick={cycleConn}
            className={`w-full flex items-center gap-2.5 ${connCfg.bg} ring-1 ${connCfg.text} text-[12.5px] font-semibold rounded-lg px-3 py-2 transition-all duration-200 cursor-pointer`}
          >
            <span className={`w-2 h-2 rounded-full shrink-0 ${connCfg.dot}`} />
            {connCfg.label}
          </button>
          {device.connectionState === "disconnected" && (
            <p className="text-[10px] text-red-500/70 mt-1.5 pl-0.5">TCP connection lost</p>
          )}
          {device.connectionState === "connected" && (
            <p className="text-[10px] text-emerald-500/60 mt-1.5 pl-0.5">Stream active</p>
          )}
          {device.connectionState === "idle" && (
            <p className="text-[10px] text-[#343a47] mt-1.5 pl-0.5">Not connected</p>
          )}
        </div>
      </Section>

      <VSep />

      {/* ④ Actions */}
      <Section label="Actions" width="flex-1">
        <div className="flex gap-2 px-5">
          {/* Clean */}
          <button
            onClick={() => onUpdate({ ...device, cleanActive: !device.cleanActive })}
            className={`flex items-center gap-1.5 text-[12.5px] font-semibold rounded-lg px-4 py-2 transition-all duration-200 cursor-pointer ${
              device.cleanActive
                ? "bg-emerald-700/25 border border-emerald-500/35 text-emerald-400 hover:bg-emerald-700/35"
                : "bg-blue-700/15 border border-blue-500/25 text-blue-400 hover:bg-blue-700/25"
            }`}
          >
            {device.cleanActive ? (
              <svg width="11" height="11" viewBox="0 0 12 12" fill="none">
                <path d="M1.5 6.5l3 3 6-6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            ) : null}
            Clean
          </button>

          {/* Auto */}
          <button
            onClick={() => onUpdate({ ...device, autoOn: !device.autoOn })}
            className={`flex items-center gap-2 text-[12.5px] font-semibold rounded-lg px-4 py-2 transition-all duration-200 cursor-pointer ${
              device.autoOn
                ? "bg-emerald-700/25 border border-emerald-500/35 text-emerald-400 hover:bg-emerald-700/35"
                : "bg-[#1c1f29] border border-[#252932] text-[#505868] hover:border-[#2e3440] hover:text-[#6b7585]"
            }`}
          >
            {/* Toggle track */}
            <span className={`relative inline-flex w-7 h-4 rounded-full transition-colors duration-200 shrink-0 ${device.autoOn ? "bg-emerald-500" : "bg-[#2a2e3a]"}`}>
              <span className={`absolute top-0.5 w-3 h-3 bg-white rounded-full shadow transition-all duration-200 ${device.autoOn ? "left-3.5" : "left-0.5"}`} />
            </span>
            Auto
          </button>
        </div>
      </Section>

      <VSep />

      {/* ⑤ Temperature */}
      <Section label="Temperature" width="w-[148px]">
        <div className="pl-4">
          <button
            className={`w-full flex items-center justify-between gap-2 bg-[#111419] border rounded-lg px-3.5 py-2 transition-all duration-200 cursor-pointer group ${
              tempAlert
                ? "border-red-600/35 hover:border-red-500/55 hover:bg-[#16101a]"
                : "border-[#252932] hover:border-[#30364a] hover:bg-[#13161f]"
            }`}
          >
            <span className={`font-mono text-[15px] font-semibold tracking-tight ${tempAlert ? "text-red-400" : "text-[#d8e0ed]"}`}>
              {device.temperature}°C
            </span>
            {tempAlert ? (
              <span className="text-[9px] font-bold tracking-wider uppercase bg-red-600/20 border border-red-500/30 text-red-400 rounded px-1.5 py-0.5 shrink-0">
                HIGH
              </span>
            ) : (
              <span className="text-[9px] text-[#343a47] group-hover:text-[#454c5a] transition-colors">
                ▲ view
              </span>
            )}
          </button>
          {tempAlert && (
            <p className="text-[10px] text-red-500/60 mt-1.5">Exceeds threshold</p>
          )}
        </div>
      </Section>
    </div>
  )
}

/* ─── App ─────────────────────────────────────────────────────────── */
export default function App() {
  const [devices, setDevices] = useState<Device[]>(seed)

  function update(id: string, d: Device) {
    setDevices(prev => prev.map(x => (x.id === id ? d : x)))
  }

  function addDevice() {
    const num = devices.length + 1
    setDevices(prev => [
      ...prev,
      { id: String(_id++), name: `Camera ${num}`, ip: "", connectionState: "idle", cleanActive: false, autoOn: false, temperature: 35 },
    ])
  }

  const connected = devices.filter(d => d.connectionState === "connected").length
  const alerts = devices.filter(d => d.temperature >= TEMP_ALERT).length
  const scrollable = devices.length >= 4

  return (
    <div className="h-full flex flex-col bg-[#12141b] font-sans text-[#d8e0ed] overflow-hidden antialiased">

      {/* ── Title Bar ───────────────────────────────────────────────── */}
      <header className="shrink-0 h-[52px] bg-[#0c0e14] border-b border-[#171a22] flex items-center px-5 gap-4 z-20">
        {/* Window controls */}
        <div className="flex items-center gap-[6px] mr-3">
          <span className="w-[12px] h-[12px] rounded-full bg-[#ff5f57] border border-[#e0443e]/40" />
          <span className="w-[12px] h-[12px] rounded-full bg-[#febc2e] border border-[#d4a02a]/40" />
          <span className="w-[12px] h-[12px] rounded-full bg-[#28c840] border border-[#23aa37]/40" />
        </div>

        {/* App identity */}
        <div className="flex items-center gap-2.5">
          {/* Icon */}
          <div className="w-7 h-7 rounded-lg bg-blue-600/20 border border-blue-500/30 flex items-center justify-center shrink-0">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" className="text-blue-400">
              <circle cx="7" cy="7" r="5.5" stroke="currentColor" strokeWidth="1.2" />
              <circle cx="7" cy="7" r="2.5" fill="currentColor" opacity="0.5" />
              <circle cx="7" cy="7" r="1" fill="currentColor" />
              <path d="M7 1.5V3M7 11v1.5M1.5 7H3M11 7h1.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
            </svg>
          </div>
          <span className="text-[14px] font-semibold text-[#c8d4e8] tracking-tight">
            Echovista Multi-Camera Controller
          </span>
          <span className="text-[10px] font-bold tracking-[0.12em] uppercase bg-blue-600/18 border border-blue-500/25 text-blue-400/90 rounded-md px-2 py-0.5 ml-0.5">
            EMCC
          </span>
        </div>

        {/* Spacer */}
        <div className="flex-1" />

        {/* Status indicators */}
        <div className="flex items-center gap-4 text-[11.5px] text-[#3e4554]">
          {alerts > 0 && (
            <>
              <div className="flex items-center gap-1.5 text-red-400/80">
                <svg width="11" height="11" viewBox="0 0 12 12" fill="none">
                  <path d="M6 1L11 10H1L6 1z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
                  <path d="M6 5v2.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
                  <circle cx="6" cy="8.5" r="0.6" fill="currentColor" />
                </svg>
                {alerts} temp alert{alerts > 1 ? "s" : ""}
              </div>
              <div className="w-px h-3.5 bg-[#1e2230]" />
            </>
          )}

          <div className="flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 status-pulse" />
            <span className="text-[#5a6270]">System Online</span>
          </div>

          <div className="w-px h-3.5 bg-[#1e2230]" />

          <span>
            <span className="text-[#b0bcce] font-semibold">{connected}</span>
            <span className="text-[#252932]"> / </span>
            <span className="text-[#4a5060]">{devices.length}</span>
            <span className="text-[#3e4554] ml-1">connected</span>
          </span>

          <div className="w-px h-3.5 bg-[#1e2230]" />

          <span className="font-mono text-[10.5px] text-[#343a47]">v2.4.1</span>
        </div>
      </header>

      {/* ── Sub-header ──────────────────────────────────────────────── */}
      <div className="shrink-0 px-8 pt-4 pb-2.5 flex items-end justify-between border-b border-[#13161e]">
        <div>
          <h1 className="text-[11px] font-semibold tracking-[0.12em] uppercase text-[#404755]">
            Device Controllers
          </h1>
          <p className="text-[11px] text-[#2a2f3c] mt-0.5">
            {devices.length} device{devices.length !== 1 ? "s" : ""} configured · click Connect to establish TCP connection
          </p>
        </div>

        <div className="flex items-center gap-5 pb-0.5">
          {[
            { dot: "bg-blue-600/70", label: "Idle" },
            { dot: "bg-emerald-600/70", label: "Connected" },
            { dot: "bg-red-600/70", label: "Disconnected" },
          ].map(({ dot, label }) => (
            <span key={label} className="flex items-center gap-1.5 text-[10.5px] text-[#343a47]">
              <span className={`w-2 h-2 rounded-full ${dot}`} />
              {label}
            </span>
          ))}
        </div>
      </div>

      {/* ── Device List ─────────────────────────────────────────────── */}
      <main
        className={`flex-1 px-8 py-4 flex flex-col gap-3 ${scrollable ? "overflow-y-auto device-scroll" : "overflow-hidden"}`}
      >
        {devices.map(device => (
          <DeviceCard key={device.id} device={device} onUpdate={d => update(device.id, d)} />
        ))}

        {/* Add device */}
        <button
          onClick={addDevice}
          className="flex items-center justify-center gap-3 rounded-xl border border-dashed border-[#1e2230] hover:border-blue-600/30 hover:bg-blue-600/[0.03] text-[#2e3340] hover:text-blue-500/70 py-4 text-[13px] font-medium transition-all duration-200 cursor-pointer group mt-1"
        >
          <span className="w-6 h-6 rounded-full border border-dashed border-[#2a2e3c] group-hover:border-blue-600/40 flex items-center justify-center text-base leading-none transition-colors">
            +
          </span>
          Add Device
        </button>

        {scrollable && (
          <p className="text-center text-[10.5px] text-[#252932] pb-1">
            Scroll to view all {devices.length} devices · supports up to 25
          </p>
        )}
      </main>
    </div>
  )
}
