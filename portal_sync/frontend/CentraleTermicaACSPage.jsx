import React, { useCallback, useEffect, useState } from "react";
import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { getToken } from "./auth";

const POLL_MS = 5000;
const ACS_HISTORY_POINTS = 180;
const ACS_HISTORY_STORAGE_KEY = "acs-history-v1";

const SENSOR_META = {
  S1: { label: "Pannelli solari", icon: "☀️", zone: "solare" },
  S2: { label: "Boiler solare (centro)", icon: "🔵", zone: "solare" },
  S3: { label: "Boiler solare (alto)", icon: "🔴", zone: "solare" },
  S4: { label: "Boiler PDC (alto)", icon: "🔴", zone: "pdc" },
  S5: { label: "Boiler PDC (basso)", icon: "🔵", zone: "pdc" },
  S6: { label: "Collettore ricircolo (in)", icon: "🔵", zone: "recirc" },
  S7: { label: "Collettore ricircolo (out)", icon: "🔴", zone: "recirc" },
};

const ZONE_LABELS = {
  solare: "Impianto solare",
  pdc: "Pompa di calore",
  recirc: "Ricircolo collettore",
};

const ALARM_LABELS = {
  ALARM_SENSORS_PANELS: "Sonde pannelli (S1/S2/S3)",
  ALARM_SENSORS_C2: "Sonde C2 (S2/S3/S4/S5)",
  ALARM_SENSORS_CR: "Sonde CR (S6/S7)",
  ALARM_S4_INVALID: "Sonda S4 (critica stop hard)",
  ALARM_C2_FB_MISMATCH: "Feedback C2",
};

const RELAY_META = {
  C2: {
    terminal: "Q0.0",
    title: "Pompa trasferimento",
    detail: "Solare → PDC",
    automation: "Automatica attiva",
    automationNote: "Controllo C2 schedulato nel firmware.",
  },
  PISCINA_PUMP: {
    terminal: "Q0.1",
    title: "Pompa piscina",
    detail: "Richiesta piscina",
    automation: "Automatica attiva",
    automationNote:
      "Block 2 schedulato: segue richiesta piscina e flag di riempimento.",
  },
  HEAT_PUMP: {
    terminal: "Q0.2",
    title: "Pompa aiuto riscaldamento",
    detail: "Supporto riscaldamento",
    automation: "Automatica attiva",
    automationNote:
      "Block 2 schedulato: segue la richiesta aiuto riscaldamento.",
  },
  CR: {
    terminal: "Q0.3",
    title: "Pompa ricircolo",
    detail: "Collettore ACS",
    automation: "Automatica attiva",
    automationNote: "Controllo ricircolo schedulato nel firmware.",
  },
  VALVE: {
    terminal: "Q0.4",
    title: "Valvola EVIE",
    detail: "Valvola motorizzata",
    automation: "Automatica attiva",
    automationNote:
      "Block 2 schedulato: si apre su richiesta piscina o riscaldamento.",
  },
  GAS_ENABLE: {
    terminal: "Q0.6",
    title: "GAS",
    detail: "Abilitazione gas",
    automation: "Automatica attiva",
    automationNote:
      "Block 2 schedulato: segue richiesta aiuto, ACS o boost piscina.",
  },
  PDC_CMD_START_ACR: {
    terminal: "Q0.7",
    title: "Avvio lavoro ACR",
    detail: "Comando PDC",
    automation: "Automatica attiva",
    automationNote:
      "Block 2 schedulato: comanda ACR quando ACS non è attiva e c'è richiesta.",
  },
};

const INPUT_META = {
  PDC_WORK_ACS: {
    terminal: "I0.0",
    source: "I0.0",
    title: "PDC lavoro ACS",
    detail: "Feedback relè NC",
  },
  PDC_HELP_REQUEST: {
    terminal: "I0.1",
    source: "I0.1",
    title: "PDC chiede aiuto",
    detail: "Feedback relè NC",
  },
  PDC_WORK_ACR: {
    terminal: "I0.2",
    source: "I0.2",
    title: "PDC lavoro ACR",
    detail: "Feedback relè NC",
  },
  HEAT_HELP_REQUEST: {
    terminal: "I0.3",
    source: "I0.3",
    title: "Aiuto riscaldamento",
    detail: "Feedback relè NC",
  },
  POOL_THERMOSTAT_CALL: {
    terminal: "I0.4",
    source: "I0.4",
    title: "Richiesta piscina",
    detail: "Feedback relè NC",
  },
};

const SENSOR_ALARM_MAP = {
  S1: "ALARM_SENSORS_PANELS",
  S2: "ALARM_SENSORS_PANELS",
  S3: "ALARM_SENSORS_PANELS",
  S4: "ALARM_S4_INVALID",
  S5: "ALARM_SENSORS_C2",
  S6: "ALARM_SENSORS_CR",
  S7: "ALARM_SENSORS_CR",
};

const WILO_STOP_DUTY_PCT = 20;
const WILO_MIN_RUN_DUTY_PCT = 23;
const WILO_MAX_RUN_DUTY_PCT = 95;
const ANTILEG_SCHEDULE_DEFAULT = {
  enabled: false,
  weekday: 6,
  time_hhmm: "03:00",
  weekday_label: "Domenica",
  next_run_at: null,
  last_trigger_at: null,
  last_result: null,
};
const ANTILEG_WEEKDAYS = [
  { value: 0, label: "Lunedi" },
  { value: 1, label: "Martedi" },
  { value: 2, label: "Mercoledi" },
  { value: 3, label: "Giovedi" },
  { value: 4, label: "Venerdi" },
  { value: 5, label: "Sabato" },
  { value: 6, label: "Domenica" },
];
const NIGHT_ECO_DEFAULT = {
  enabled: false,
  start_hhmm: "23:00",
  end_hhmm: "06:00",
  day_pdc_target_c: 50,
  night_pdc_target_c: 45,
  day_recirc_target_c: 45,
  night_recirc_target_c: 40,
  active_mode: "day",
  night_active: false,
  last_applied_mode: null,
  last_applied_at: null,
  last_result: null,
};

function hasOwn(obj, key) {
  return !!obj && Object.prototype.hasOwnProperty.call(obj, key);
}

function normalizeNightEco(value) {
  const data = value && typeof value === "object" ? value : {};
  const numOr = (raw, fallback) => {
    const num = Number(raw);
    return Number.isFinite(num) ? num : fallback;
  };
  return {
    enabled: Boolean(data.enabled),
    start_hhmm: String(data.start_hhmm || NIGHT_ECO_DEFAULT.start_hhmm),
    end_hhmm: String(data.end_hhmm || NIGHT_ECO_DEFAULT.end_hhmm),
    day_pdc_target_c: numOr(
      data.day_pdc_target_c,
      NIGHT_ECO_DEFAULT.day_pdc_target_c,
    ),
    night_pdc_target_c: numOr(
      data.night_pdc_target_c,
      NIGHT_ECO_DEFAULT.night_pdc_target_c,
    ),
    day_recirc_target_c: numOr(
      data.day_recirc_target_c,
      NIGHT_ECO_DEFAULT.day_recirc_target_c,
    ),
    night_recirc_target_c: numOr(
      data.night_recirc_target_c,
      NIGHT_ECO_DEFAULT.night_recirc_target_c,
    ),
    active_mode:
      data.active_mode === "night" ? "night" : NIGHT_ECO_DEFAULT.active_mode,
    night_active: Boolean(data.night_active),
    last_applied_mode:
      data.last_applied_mode === "night" || data.last_applied_mode === "day"
        ? data.last_applied_mode
        : null,
    last_applied_at: Number.isFinite(Number(data.last_applied_at))
      ? Number(data.last_applied_at)
      : null,
    last_result: data.last_result ? String(data.last_result) : null,
  };
}

function normalizeOtaStatus(value) {
  const data = value && typeof value === "object" ? value : {};
  const numOrNull = (raw) => {
    const num = Number(raw);
    return Number.isFinite(num) ? num : null;
  };
  return {
    enabled: Boolean(data.enabled),
    current_version: data.current_version ? String(data.current_version) : "unknown",
    current_build: data.current_build ? String(data.current_build) : null,
    current_partition: data.current_partition
      ? String(data.current_partition)
      : null,
    state: data.state ? String(data.state) : "idle",
    message: data.message ? String(data.message) : null,
    target_version: data.target_version ? String(data.target_version) : null,
    manifest_url: data.manifest_url ? String(data.manifest_url) : null,
    firmware_url: data.firmware_url ? String(data.firmware_url) : null,
    target_partition: data.target_partition
      ? String(data.target_partition)
      : null,
    bytes_written: Number.isFinite(Number(data.bytes_written))
      ? Number(data.bytes_written)
      : 0,
    total_bytes: Number.isFinite(Number(data.total_bytes))
      ? Number(data.total_bytes)
      : 0,
    started_at: numOrNull(data.started_at),
    finished_at: numOrNull(data.finished_at),
    last_error: data.last_error ? String(data.last_error) : null,
    last_result: data.last_result ? String(data.last_result) : null,
    last_success_version: data.last_success_version
      ? String(data.last_success_version)
      : null,
    last_success_partition: data.last_success_partition
      ? String(data.last_success_partition)
      : null,
  };
}

function clampPct(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return 0;
  return Math.max(0, Math.min(100, Math.round(num)));
}

function normalizeWiloCommandPct(value) {
  const duty = clampPct(value);
  if (duty <= WILO_STOP_DUTY_PCT) return WILO_STOP_DUTY_PCT;
  return Math.max(WILO_MIN_RUN_DUTY_PCT, Math.min(WILO_MAX_RUN_DUTY_PCT, duty));
}

function getWiloSpeedPct(wiloDutyPct) {
  const duty = clampPct(wiloDutyPct);
  if (duty <= WILO_STOP_DUTY_PCT) return 0;

  const boundedDuty = Math.max(
    WILO_MIN_RUN_DUTY_PCT,
    Math.min(WILO_MAX_RUN_DUTY_PCT, duty),
  );
  const speedPct =
    1 +
    ((boundedDuty - WILO_MIN_RUN_DUTY_PCT) * 99) /
      (WILO_MAX_RUN_DUTY_PCT - WILO_MIN_RUN_DUTY_PCT);
  return Math.round(speedPct);
}

function getWiloState(wiloDutyPct) {
  const duty = clampPct(wiloDutyPct);
  const speedPct = getWiloSpeedPct(duty);

  if (duty === 0) {
    return {
      duty,
      speedPct: 0,
      running: false,
      label: "OFF",
      detail: "uscita non inizializzata",
    };
  }

  if (duty <= WILO_STOP_DUTY_PCT) {
    return {
      duty,
      speedPct: 0,
      running: false,
      label: "STOP",
      detail: "standby Wilo PWM2",
    };
  }

  return {
    duty,
    speedPct,
    running: true,
    label: `${duty}%`,
    detail: `velocita stimata ${speedPct}%`,
  };
}

function fmt(val, unit = "°C") {
  if (val === null || val === undefined) return "—";
  return `${Number(val).toFixed(1)} ${unit}`;
}

function formatAgo(ts) {
  if (!ts) return "—";
  const diff = Math.max(0, Date.now() / 1000 - ts);
  if (diff < 60) return `${Math.floor(diff)}s fa`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m fa`;
  return `${Math.floor(diff / 3600)}h fa`;
}

function formatDate(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("it-IT");
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "—";
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(2)} MiB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${Math.round(bytes)} B`;
}

function formatHistoryTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleTimeString("it-IT", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function normalizeAntilegSchedule(schedule) {
  const next = schedule && typeof schedule === "object" ? schedule : {};
  const weekday = Number(next.weekday);
  return {
    enabled: Boolean(next.enabled),
    weekday: Number.isInteger(weekday) ? weekday : 6,
    time_hhmm:
      typeof next.time_hhmm === "string" && next.time_hhmm
        ? next.time_hhmm
        : "03:00",
    weekday_label:
      typeof next.weekday_label === "string" && next.weekday_label
        ? next.weekday_label
        : "Domenica",
    next_run_at: next.next_run_at ?? null,
    last_trigger_at: next.last_trigger_at ?? null,
    last_result:
      typeof next.last_result === "string" ? next.last_result : null,
  };
}

function averageDefined(values) {
  const valid = values.filter((value) => Number.isFinite(value));
  if (!valid.length) return null;
  return valid.reduce((sum, value) => sum + value, 0) / valid.length;
}

function loadStoredAcsHistory() {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(ACS_HISTORY_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item) => item && Number.isFinite(Number(item.ts)))
      .slice(-ACS_HISTORY_POINTS);
  } catch (_err) {
    return [];
  }
}

function TempCard({ id, value, alarm }) {
  const meta = SENSOR_META[id] || {};
  const missing = value === null || value === undefined;
  const color =
    missing || alarm
      ? "var(--danger)"
      : value > 80
        ? "#f44336"
        : value > 60
          ? "var(--warn)"
          : value > 40
            ? "var(--ok)"
            : "#64b5f6";

  return (
    <div
      className="card"
      style={{
        borderLeft: `4px solid ${color}`,
        display: "flex",
        flexDirection: "column",
        gap: 4,
        padding: "10px 14px",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
          {meta.icon} {id} – {meta.label}
        </span>
        {(missing || alarm) && (
          <span
            style={{ fontSize: 11, color: "var(--danger)", fontWeight: 600 }}
          >
            {alarm ? "⚠ ALLARME" : "INVALID"}
          </span>
        )}
      </div>
      <div style={{ fontSize: 28, fontWeight: 700, color }}>
        {missing ? "— °C" : fmt(value)}
      </div>
    </div>
  );
}

function ActuatorCard({ label, sublabel, active, children }) {
  const color = active ? "var(--ok)" : "var(--text-muted)";
  return (
    <div
      className="card"
      style={{
        borderLeft: `4px solid ${color}`,
        padding: "10px 14px",
        display: "flex",
        flexDirection: "column",
        gap: 4,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
        }}
      >
        <span style={{ fontWeight: 600 }}>{label}</span>
        <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
          {sublabel}
        </span>
      </div>
      {children}
    </div>
  );
}

function AlarmRow({ id, active }) {
  if (!active) return null;
  return (
    <div
      style={{
        background: "color-mix(in oklab, var(--danger) 18%, var(--surface))",
        border: "1px solid var(--danger)",
        borderRadius: 8,
        padding: "8px 12px",
        fontSize: 13,
        color: "var(--danger)",
        fontWeight: 600,
        display: "flex",
        alignItems: "center",
        gap: 8,
      }}
    >
      ⚠ {ALARM_LABELS[id] || id}
    </div>
  );
}

function SectionTitle({ children }) {
  return (
    <h3
      style={{
        margin: "24px 0 10px",
        fontSize: 14,
        fontWeight: 700,
        textTransform: "uppercase",
        letterSpacing: "0.08em",
        color: "var(--text-muted)",
        borderBottom: "1px solid var(--border)",
        paddingBottom: 6,
      }}
    >
      {children}
    </h3>
  );
}

function WiloDutyBar({ wiloDutyPct }) {
  const wiloState = getWiloState(wiloDutyPct);
  return (
    <div style={{ marginTop: 4 }}>
      <div
        style={{
          height: 10,
          background: "var(--surface-soft)",
          borderRadius: 5,
          overflow: "hidden",
          border: "1px solid var(--border)",
        }}
      >
        <div
          style={{
            width: `${wiloState.speedPct}%`,
            height: "100%",
            background: wiloState.running ? "var(--ok)" : "var(--border)",
            transition: "width 0.4s ease",
          }}
        />
      </div>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
        {wiloState.running
          ? `Duty Wilo PWM2: ${wiloState.duty}% · velocita stimata ${wiloState.speedPct}%`
          : `Duty Wilo PWM2: ${wiloState.duty}% · ${wiloState.detail}`}
      </div>
    </div>
  );
}

function ManualModeCard({ manualMode, online, busy, onToggle }) {
  return (
    <div
      className="card"
      style={{
        padding: "14px 16px",
        display: "flex",
        flexDirection: "column",
        gap: 10,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: 10,
          flexWrap: "wrap",
        }}
      >
        <div>
          <div style={{ fontWeight: 700 }}>Modalità manuale</div>
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
            Con manuale ON, tutte le uscite della centrale passano sotto comando
            diretto dal portale. Con manuale OFF, tutte le logiche automatiche
            tornano attive, incluso il Block 2 piscina/riscaldamento.
          </div>
        </div>
        <div
          style={{
            fontSize: 12,
            fontWeight: 700,
            color: manualMode ? "var(--warn)" : "var(--text-muted)",
          }}
        >
          {manualMode ? "MANUALE ATTIVA" : "AUTOMATICO"}
        </div>
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!online || busy || manualMode}
          onClick={() => onToggle(true)}
        >
          {busy && !manualMode ? "..." : "Abilita manuale"}
        </button>
        <button
          type="button"
          className="btn"
          disabled={!online || busy || !manualMode}
          onClick={() => onToggle(false)}
        >
          {busy && manualMode ? "..." : "Disabilita manuale"}
        </button>
      </div>
    </div>
  );
}

function RuntimeStatusCard() {
  return (
    <div
      className="card"
      style={{
        padding: "14px 16px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <div style={{ fontWeight: 700 }}>Firmware attivo nel progetto</div>
      <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
        Automatiche attive: C1 pannelli, C2 trasferimento solare, CR ricircolo e
        Block 2 piscina/riscaldamento.
      </div>
      <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
        Q0.1, Q0.2, Q0.4, Q0.6 e Q0.7 ora sono governate dalla logica Block 2
        con gli ingressi piscina/riscaldamento e con il flag
        <code>pool_just_filled</code>.
      </div>
      <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
        Quando abiliti la modalità manuale, anche C1, C2, CR e le uscite Block 2
        smettono di seguire la logica automatica e rispondono solo ai comandi
        del portale.
      </div>
      <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
        C1 usa l'uscita PWM <strong>A0.5</strong> con duty Wilo PWM2 diretto:
        20% standby, 23% minima marcia, 95% massima velocità.
      </div>
    </div>
  );
}

function FirmwareOTACard({ online, firmwareVersion, ota, busy, onStart }) {
  const activeStates = new Set([
    "queued",
    "downloading",
    "writing",
    "applying",
    "restarting",
  ]);
  const active = activeStates.has(ota.state);
  const tone =
    ota.state === "error"
      ? "var(--danger)"
      : active
        ? "var(--warn)"
        : "var(--ok)";
  const progressLabel =
    ota.total_bytes > 0
      ? `${formatBytes(ota.bytes_written)} / ${formatBytes(ota.total_bytes)}`
      : ota.bytes_written > 0
        ? formatBytes(ota.bytes_written)
        : "—";

  return (
    <div
      className="card"
      style={{
        padding: "14px 16px",
        display: "flex",
        flexDirection: "column",
        gap: 10,
      }}
    >
      <div>
        <div style={{ fontWeight: 700 }}>Aggiornamento firmware OTA</div>
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
          Il PLC scarica un firmware <code>.app-bin</code> via backend ACS, lo
          scrive nella partizione OTA successiva e poi si riavvia. Avvia
          l&apos;update solo in una finestra operativa sicura.
        </div>
      </div>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 12 }}>
        <div>
          <div style={{ color: "var(--text-muted)" }}>Versione PLC</div>
          <div style={{ fontWeight: 700 }}>{firmwareVersion || "unknown"}</div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)" }}>Partizione attiva</div>
          <div style={{ fontWeight: 700 }}>{ota.current_partition || "—"}</div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)" }}>Stato OTA</div>
          <div style={{ fontWeight: 700, color: tone }}>{ota.state || "idle"}</div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)" }}>Target</div>
          <div style={{ fontWeight: 700 }}>{ota.target_version || "—"}</div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)" }}>Download</div>
          <div style={{ fontWeight: 700 }}>{progressLabel}</div>
        </div>
      </div>
      <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
        {ota.message || "Backend pronto a servire il firmware OTA corrente."}
      </div>
      {ota.target_partition && (
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
          Partizione target: <code>{ota.target_partition}</code>
        </div>
      )}
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 12 }}>
        <div>
          <div style={{ color: "var(--text-muted)" }}>Inizio</div>
          <div style={{ fontWeight: 700 }}>{formatDate(ota.started_at)}</div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)" }}>Fine</div>
          <div style={{ fontWeight: 700 }}>{formatDate(ota.finished_at)}</div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)" }}>Ultimo esito</div>
          <div
            style={{
              fontWeight: 700,
              color:
                ota.last_result === "error"
                  ? "var(--danger)"
                  : ota.last_result === "success"
                    ? "var(--ok)"
                    : "var(--text)",
            }}
          >
            {ota.last_result || "—"}
          </div>
        </div>
      </div>
      {ota.last_error && (
        <div style={{ fontSize: 12, color: "var(--danger)" }}>
          Ultimo errore: {ota.last_error}
        </div>
      )}
      {ota.last_success_partition && (
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
          Ultima partizione valida: <code>{ota.last_success_partition}</code>
        </div>
      )}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!online || busy || active || !ota.enabled}
          onClick={onStart}
        >
          {busy ? "..." : "Avvia OTA dal backend"}
        </button>
      </div>
    </div>
  );
}

function RelayControlCard({
  name,
  meta,
  actual,
  requested,
  available,
  manualMode,
  online,
  busy,
  onCommand,
}) {
  const active = actual ?? false;
  return (
    <div
      className="card"
      style={{
        borderLeft: `4px solid ${active ? "var(--ok)" : "var(--text-muted)"}`,
        padding: "12px 14px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <div>
        <div style={{ fontWeight: 700 }}>
          {meta.terminal} – {meta.title}
        </div>
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
          {meta.detail}
        </div>
      </div>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 12 }}>
        <div>
          <div style={{ color: "var(--text-muted)" }}>Stato uscita</div>
          <div
            style={{
              fontWeight: 700,
              color: active ? "var(--ok)" : "var(--text-muted)",
            }}
          >
            {active ? "ON" : "OFF"}
          </div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)" }}>Richiesta manuale</div>
          <div style={{ fontWeight: 700 }}>{requested ? "ON" : "OFF"}</div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)" }}>Disponibilità</div>
          <div
            style={{
              fontWeight: 700,
              color: available ? "var(--ok)" : "var(--danger)",
            }}
          >
            {available ? "OK" : "Non disponibile"}
          </div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)" }}>Gestione firmware</div>
          <div
            style={{
              fontWeight: 700,
              color: meta.automation.startsWith("Automatica")
                ? "var(--ok)"
                : "var(--warn)",
            }}
          >
            {meta.automation}
          </div>
        </div>
      </div>
      <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
        {meta.automationNote}
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!online || !manualMode || !available || busy}
          onClick={() => onCommand(name, true)}
        >
          {busy ? "..." : "ON"}
        </button>
        <button
          type="button"
          className="btn"
          disabled={!online || !manualMode || !available || busy}
          onClick={() => onCommand(name, false)}
        >
          OFF
        </button>
      </div>
    </div>
  );
}

function InputCard({ name, meta, inputs }) {
  const logicalKnown = hasOwn(inputs, name);
  const rawKnown = hasOwn(inputs, meta.source);
  const logicalValue = logicalKnown ? Boolean(inputs[name]) : null;
  const rawValue = rawKnown ? Boolean(inputs[meta.source]) : null;
  const active = logicalValue === true;

  return (
    <div
      className="card"
      style={{
        borderLeft: `4px solid ${active ? "var(--ok)" : "var(--text-muted)"}`,
        padding: "12px 14px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <div>
        <div style={{ fontWeight: 700 }}>
          {meta.terminal} – {meta.title}
        </div>
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
          {meta.detail}
        </div>
      </div>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 12 }}>
        <div>
          <div style={{ color: "var(--text-muted)" }}>Stato logico</div>
          <div
            style={{
              fontWeight: 700,
              color: active ? "var(--ok)" : "var(--text-muted)",
            }}
          >
            {logicalKnown ? (logicalValue ? "ATTIVO" : "SPENTO") : "—"}
          </div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)" }}>Contatto NC raw</div>
          <div style={{ fontWeight: 700 }}>
            {rawKnown ? (rawValue ? "CHIUSO" : "APERTO") : "—"}
          </div>
        </div>
      </div>
      <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
        Firmware allineato a contatti NC: chiuso = riposo, aperto = segnale
        attivo.
      </div>
    </div>
  );
}

function SetpointCard({
  id,
  meta,
  value,
  draft,
  disabled,
  busy,
  onDraftChange,
  onApply,
}) {
  return (
    <div
      className="card"
      style={{
        padding: "12px 14px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <div>
        <div style={{ fontWeight: 700 }}>{meta?.label || id}</div>
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
          Attuale: {fmt(value)} · Range {meta?.min ?? "—"} / {meta?.max ?? "—"}
        </div>
      </div>
      <div
        style={{
          display: "flex",
          gap: 8,
          flexWrap: "wrap",
          alignItems: "center",
        }}
      >
        <input
          type="number"
          min={meta?.min}
          max={meta?.max}
          step={meta?.step ?? 0.5}
          value={draft ?? ""}
          disabled={disabled}
          onChange={(e) => onDraftChange(id, e.target.value)}
          style={{
            width: 110,
            padding: "8px 10px",
            borderRadius: 8,
            border: "1px solid var(--border)",
            background: "var(--surface-soft)",
            color: "var(--text)",
          }}
        />
        <button
          type="button"
          className="btn btn-primary"
          disabled={disabled || busy}
          onClick={() => onApply(id)}
        >
          {busy ? "..." : "Applica"}
        </button>
      </div>
    </div>
  );
}

export default function CentraleTermicaACSPage() {
  const [data, setData] = useState(null);
  const [history, setHistory] = useState(() => loadStoredAcsHistory());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [cmdMsg, setCmdMsg] = useState("");
  const [busyKey, setBusyKey] = useState("");
  const [antilegPending, setAntilegPending] = useState(false);
  const [antilegScheduleDraft, setAntilegScheduleDraft] = useState(
    ANTILEG_SCHEDULE_DEFAULT,
  );
  const [antilegScheduleDirty, setAntilegScheduleDirty] = useState(false);
  const [nightEcoDraft, setNightEcoDraft] = useState(NIGHT_ECO_DEFAULT);
  const [nightEcoDirty, setNightEcoDirty] = useState(false);
  const [pwmDraft, setPwmDraft] = useState("");
  const [setpointDrafts, setSetpointDrafts] = useState({});

  const fetchState = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const res = await fetch("/api/acs/state", {
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        setError(text || `Errore ${res.status}`);
        return;
      }
      const next = await res.json();
      setData(next);
      setHistory((prev) => {
        const ts = Number(next?.received_at || Date.now() / 1000);
        if (!Number.isFinite(ts)) return prev;

        const last = prev[prev.length - 1];
        if (last && Math.abs(last.ts - ts) < 1) return prev;

        const panelTemp = Number(next?.temps?.S1);
        const boilerSolarTemp = averageDefined([
          Number(next?.temps?.S2),
          Number(next?.temps?.S3),
        ]);
        const c1Duty = Number(next?.c1_wilo_duty_pct ?? next?.c1_duty ?? 0);
        const c1Active = Boolean(next?.c1_active ?? c1Duty > WILO_STOP_DUTY_PCT);

        const point = {
          ts,
          timeLabel: formatHistoryTime(ts),
          panel_temp_c: Number.isFinite(panelTemp) ? panelTemp : null,
          boiler_solar_temp_c: boilerSolarTemp,
          c1_active: c1Active,
          c1_active_value: c1Active ? 1 : 0,
        };
        return [...prev.slice(-(ACS_HISTORY_POINTS - 1)), point];
      });
      setError("");
    } catch (_err) {
      if (!silent) setError("Errore di rete");
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchState();
    const timer = setInterval(() => fetchState(true), POLL_MS);
    return () => clearInterval(timer);
  }, [fetchState]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(
        ACS_HISTORY_STORAGE_KEY,
        JSON.stringify(history.slice(-ACS_HISTORY_POINTS)),
      );
    } catch (_err) {
      // ignore localStorage quota / private mode errors
    }
  }, [history]);

  useEffect(() => {
    if (!data) return;
    setPwmDraft((prev) =>
      prev === ""
        ? String(
            normalizeWiloCommandPct(
              data.manual_c1_wilo_duty_pct ??
                data.manual_pwm_duty ??
                data.c1_wilo_duty_pct ??
                data.c1_duty ??
                WILO_STOP_DUTY_PCT,
            ),
          )
        : prev,
    );
    setSetpointDrafts((prev) => {
      const next = { ...prev };
      let changed = false;
      Object.entries(data.setpoints ?? {}).forEach(([key, value]) => {
        if (!(key in next)) {
          next[key] = String(value ?? "");
          changed = true;
        }
      });
      return changed ? next : prev;
    });
  }, [data]);

  useEffect(() => {
    if (!data?.antileg_schedule || antilegScheduleDirty) return;
    setAntilegScheduleDraft(normalizeAntilegSchedule(data.antileg_schedule));
  }, [data, antilegScheduleDirty]);

  useEffect(() => {
    if (!data?.night_eco || nightEcoDirty) return;
    setNightEcoDraft(normalizeNightEco(data.night_eco));
  }, [data, nightEcoDirty]);

  const postCommand = useCallback(
    async (key, url, body, successMsg) => {
      setBusyKey(key);
      setCmdMsg("");
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${getToken()}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(body),
        });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok) {
          setCmdMsg(`Errore: ${payload.detail || res.status}`);
          return false;
        }
        setCmdMsg(successMsg);
        fetchState(true);
        return true;
      } catch (_err) {
        setCmdMsg("Errore di rete nel comando.");
        return false;
      } finally {
        setBusyKey("");
      }
    },
    [fetchState],
  );

  const sendAntileg = async (value) => {
    setAntilegPending(true);
    setCmdMsg("");
    try {
      const res = await fetch("/api/acs/antileg", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${getToken()}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ request: value }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) setCmdMsg(`Errore: ${payload.detail || res.status}`);
      else
        setCmdMsg(
          value
            ? "Ciclo antilegionella avviato."
            : "Richiesta antilegionella annullata.",
        );
      fetchState(true);
    } catch (_err) {
      setCmdMsg("Errore di rete nel comando.");
    } finally {
      setAntilegPending(false);
    }
  };

  const sendOta = async () => {
    setCmdMsg("");
    setBusyKey("ota");
    try {
      const res = await fetch("/api/acs/ota", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${getToken()}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ force: false }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) {
        setCmdMsg(`Errore: ${payload.detail || res.status}`);
        return;
      }
      const targetVersion = payload?.ota?.target_version;
      setCmdMsg(
        targetVersion
          ? `OTA firmware accodata verso ${targetVersion}.`
          : "OTA accodata al PLC.",
      );
      fetchState(true);
    } catch (_err) {
      setCmdMsg("Errore di rete nel comando.");
    } finally {
      setBusyKey("");
    }
  };

  const sendManualMode = (enabled) =>
    postCommand(
      "manual-mode",
      "/api/acs/manual-mode",
      { enabled },
      enabled
        ? "Modalità manuale abilitata."
        : "Modalità manuale disabilitata.",
    );

  const sendRelay = (name, value) =>
    postCommand(
      `relay-${name}`,
      "/api/acs/relay",
      { name, state: value },
      `${name} impostato su ${value ? "ON" : "OFF"}.`,
    );

  const sendPWM = async () => {
    const inputWiloDutyPct = Number(pwmDraft);
    if (
      !Number.isFinite(inputWiloDutyPct) ||
      inputWiloDutyPct < 0 ||
      inputWiloDutyPct > 100
    ) {
      setCmdMsg("Errore: duty Wilo PWM2 non valido (0-100).");
      return;
    }
    const wiloDutyPct = normalizeWiloCommandPct(inputWiloDutyPct);
    const ok = await postCommand(
      "pwm",
      "/api/acs/pwm",
      { duty: wiloDutyPct },
      `Duty Wilo PWM2 C1 impostato a ${wiloDutyPct}%.`,
    );
    if (ok) setPwmDraft(String(wiloDutyPct));
  };

  const updateSetpointDraft = (key, value) => {
    setSetpointDrafts((prev) => ({ ...prev, [key]: value }));
  };

  const sendSetpoint = async (key) => {
    const raw = setpointDrafts[key];
    const value = Number(raw);
    if (!Number.isFinite(value)) {
      setCmdMsg(`Errore: setpoint ${key} non valido.`);
      return;
    }
    const ok = await postCommand(
      `setpoint-${key}`,
      "/api/acs/setpoint",
      { key, value },
      `Setpoint ${key} aggiornato.`,
    );
    if (ok) {
      setSetpointDrafts((prev) => ({ ...prev, [key]: String(value) }));
    }
  };

  const sendPoolJustFilled = (enabled) =>
    postCommand(
      "pool-just-filled",
      "/api/acs/pool-just-filled",
      { enabled },
      enabled
        ? "Flag piscina appena riempita attivato."
        : "Flag piscina appena riempita azzerato.",
    );

  const sendAntilegSchedule = async () => {
    const weekday = Number(antilegScheduleDraft.weekday);
    const time_hhmm = String(antilegScheduleDraft.time_hhmm || "").trim();
    if (!Number.isInteger(weekday) || weekday < 0 || weekday > 6) {
      setCmdMsg("Errore: giorno antilegionella non valido.");
      return;
    }
    const match = /^(\d{2}):(\d{2})$/.exec(time_hhmm);
    if (!match) {
      setCmdMsg("Errore: orario antilegionella non valido.");
      return;
    }
    const hours = Number(match[1]);
    const minutes = Number(match[2]);
    if (
      !Number.isInteger(hours) ||
      !Number.isInteger(minutes) ||
      hours < 0 ||
      hours > 23 ||
      minutes < 0 ||
      minutes > 59
    ) {
      setCmdMsg("Errore: orario antilegionella non valido.");
      return;
    }
    const ok = await postCommand(
      "antileg-schedule",
      "/api/acs/antileg-schedule",
      {
        enabled: Boolean(antilegScheduleDraft.enabled),
        weekday,
        time_hhmm,
      },
      "Programmazione antilegionella aggiornata.",
    );
    if (ok) {
      setAntilegScheduleDirty(false);
      fetchState(true);
    }
  };

  const sendNightEcoSchedule = async () => {
    const start_hhmm = String(nightEcoDraft.start_hhmm || "").trim();
    const end_hhmm = String(nightEcoDraft.end_hhmm || "").trim();
    const matchStart = /^(\d{2}):(\d{2})$/.exec(start_hhmm);
    const matchEnd = /^(\d{2}):(\d{2})$/.exec(end_hhmm);
    if (!matchStart || !matchEnd) {
      setCmdMsg("Errore: orario eco notte non valido.");
      return;
    }
    const startHours = Number(matchStart[1]);
    const startMinutes = Number(matchStart[2]);
    const endHours = Number(matchEnd[1]);
    const endMinutes = Number(matchEnd[2]);
    if (
      !Number.isInteger(startHours) ||
      !Number.isInteger(startMinutes) ||
      !Number.isInteger(endHours) ||
      !Number.isInteger(endMinutes) ||
      startHours < 0 ||
      startHours > 23 ||
      endHours < 0 ||
      endHours > 23 ||
      startMinutes < 0 ||
      startMinutes > 59 ||
      endMinutes < 0 ||
      endMinutes > 59
    ) {
      setCmdMsg("Errore: orario eco notte non valido.");
      return;
    }

    const values = {
      day_pdc_target_c: Number(nightEcoDraft.day_pdc_target_c),
      night_pdc_target_c: Number(nightEcoDraft.night_pdc_target_c),
      day_recirc_target_c: Number(nightEcoDraft.day_recirc_target_c),
      night_recirc_target_c: Number(nightEcoDraft.night_recirc_target_c),
    };
    if (Object.values(values).some((value) => !Number.isFinite(value))) {
      setCmdMsg("Errore: setpoint eco notte non validi.");
      return;
    }
    if (start_hhmm === end_hhmm) {
      setCmdMsg("Errore: inizio e fine fascia notte non possono coincidere.");
      return;
    }
    if (values.night_pdc_target_c > values.day_pdc_target_c) {
      setCmdMsg("Errore: target PDC notte deve essere <= del target giorno.");
      return;
    }
    if (values.night_recirc_target_c > values.day_recirc_target_c) {
      setCmdMsg(
        "Errore: target ricircolo notte deve essere <= del target giorno.",
      );
      return;
    }

    const ok = await postCommand(
      "night-eco",
      "/api/acs/night-eco",
      {
        enabled: Boolean(nightEcoDraft.enabled),
        start_hhmm,
        end_hhmm,
        day_pdc_target_c: values.day_pdc_target_c,
        night_pdc_target_c: values.night_pdc_target_c,
        day_recirc_target_c: values.day_recirc_target_c,
        night_recirc_target_c: values.night_recirc_target_c,
      },
      "Programmazione eco notte aggiornata.",
    );
    if (ok) {
      setNightEcoDirty(false);
      fetchState(true);
    }
  };

  if (loading && !data) {
    return <div style={{ padding: 24 }}>Caricamento...</div>;
  }

  if (error && !data) {
    return (
      <div style={{ padding: 24 }}>
        <h2>Centrale termica ACS</h2>
        <div style={{ color: "var(--danger)" }}>{error}</div>
      </div>
    );
  }

  const temps = data?.temps ?? {};
  const alarms = data?.alarms ?? {};
  const inputs = data?.inputs ?? {};
  const relays = data?.relays ?? {};
  const relayAvailable = data?.relay_available ?? {};
  const manualRelays = data?.manual_relays ?? {};
  const ota = normalizeOtaStatus(data?.ota);
  const setpoints = data?.setpoints ?? {};
  const setpointMeta = data?.setpoint_meta ?? {};
  const c1WiloDutyPct = data?.c1_wilo_duty_pct ?? data?.c1_duty ?? 0;
  const c1Latch = data?.c1_latch ?? false;
  const crEmerg = data?.cr_emerg ?? false;
  const manualMode = data?.manual_mode ?? false;
  const manualWiloDutyPct = normalizeWiloCommandPct(
    data?.manual_c1_wilo_duty_pct ??
      data?.manual_pwm_duty ??
      WILO_STOP_DUTY_PCT,
  );
  const c1WiloState = getWiloState(c1WiloDutyPct);
  const manualWiloState = getWiloState(manualWiloDutyPct);
  const poolJustFilled = data?.pool_just_filled ?? false;
  const online = data?.online ?? false;
  const antilegOk = data?.antileg_ok ?? false;
  const antilegOkTs = data?.antileg_ok_ts ?? null;
  const antilegRequest = data?.antileg_request ?? false;
  const antilegSchedule = normalizeAntilegSchedule(data?.antileg_schedule);
  const nightEco = normalizeNightEco(data?.night_eco);
  const receivedAt = data?.received_at ?? null;
  const firmwareVersion = String(
    data?.firmware_build ??
      ota.current_build ??
      data?.firmware_version ??
      ota.current_version ??
      "unknown",
  );
  const hasAlarm = Object.values(alarms).some(Boolean);
  const zones = ["solare", "pdc", "recirc"];
  const sensorsByZone = zones.reduce((acc, zone) => {
    acc[zone] = Object.entries(SENSOR_META)
      .filter(([, meta]) => meta.zone === zone)
      .map(([id]) => id);
    return acc;
  }, {});

  return (
    <div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          flexWrap: "wrap",
          gap: 8,
          marginBottom: 4,
        }}
      >
        <h2 style={{ margin: 0 }}>Centrale termica ACS</h2>
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "flex-end",
            gap: 4,
          }}
        >
          <span
            style={{
              fontSize: 12,
              fontWeight: 700,
              color: online ? "var(--ok)" : "var(--danger)",
              background: online
                ? "color-mix(in oklab, var(--ok) 15%, var(--surface))"
                : "color-mix(in oklab, var(--danger) 15%, var(--surface))",
              border: `1px solid ${online ? "var(--ok)" : "var(--danger)"}`,
              borderRadius: 20,
              padding: "3px 10px",
            }}
          >
            {online ? "● ONLINE" : "● OFFLINE"}
          </span>
          <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
            Ultimo dato: {formatAgo(receivedAt)}
          </span>
        </div>
      </div>

      <p
        style={{ margin: "4px 0 0", color: "var(--text-muted)", fontSize: 13 }}
      >
        Monitoraggio e comando manuale della centrale termica ACS. Aggiornamento
        ogni {POLL_MS / 1000}s.
      </p>

      {error && (
        <div style={{ marginTop: 8, color: "var(--danger)", fontSize: 13 }}>
          {error}
        </div>
      )}

      {hasAlarm && (
        <>
          <SectionTitle>Allarmi sensori</SectionTitle>
          <div style={{ display: "grid", gap: 6 }}>
            {Object.entries(alarms).map(([id, active]) => (
              <AlarmRow key={id} id={id} active={active} />
            ))}
          </div>
        </>
      )}

      <SectionTitle>Stato firmware</SectionTitle>
      <RuntimeStatusCard />
      <FirmwareOTACard
        online={online}
        firmwareVersion={firmwareVersion}
        ota={ota}
        busy={busyKey === "ota"}
        onStart={sendOta}
      />

      <SectionTitle>Confronto solare / C1</SectionTitle>
      <div
        className="card"
        style={{
          padding: "14px 16px",
          display: "flex",
          flexDirection: "column",
          gap: 10,
        }}
      >
        <div>
          <div style={{ fontWeight: 700 }}>
            Temperatura pannelli vs boiler solare e stato C1
          </div>
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
            Storico live della sessione corrente: S1 confrontata con la media di
            S2/S3. La linea verde mostra quando C1 era ON o OFF e lo storico
            resta salvato anche dopo il refresh della pagina.
          </div>
        </div>
        <div style={{ width: "100%", height: 320 }}>
          <ResponsiveContainer>
            <ComposedChart
              data={history}
              margin={{ top: 8, right: 18, left: 0, bottom: 8 }}
            >
              <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" />
              <XAxis dataKey="timeLabel" minTickGap={24} />
              <YAxis yAxisId="temp" unit="°C" />
              <YAxis
                yAxisId="state"
                orientation="right"
                domain={[0, 1]}
                ticks={[0, 1]}
                tickFormatter={(value) => (value >= 1 ? "ON" : "OFF")}
              />
              <Tooltip
                formatter={(value, name) => {
                  if (name === "Stato C1") return [value >= 1 ? "ON" : "OFF", name];
                  return [fmt(value), name];
                }}
                labelFormatter={(label, payload) => {
                  const item = payload && payload[0] ? payload[0].payload : null;
                  return item
                    ? `${label} · ${item.c1_active ? "C1 attiva" : "C1 ferma"}`
                    : label;
                }}
              />
              <Legend />
              <Line
                yAxisId="temp"
                type="monotone"
                dataKey="panel_temp_c"
                name="Pannelli solari (S1)"
                stroke="#f4b400"
                dot={false}
                strokeWidth={2}
              />
              <Line
                yAxisId="temp"
                type="monotone"
                dataKey="boiler_solar_temp_c"
                name="Boiler solare medio (S2/S3)"
                stroke="#64b5f6"
                dot={false}
                strokeWidth={2}
              />
              <Line
                yAxisId="state"
                type="stepAfter"
                dataKey="c1_active_value"
                name="Stato C1"
                stroke="#4caf50"
                dot={false}
                strokeWidth={2}
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </div>

      <SectionTitle>Attuatori</SectionTitle>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
          gap: 10,
        }}
      >
        <ActuatorCard
          label="A0.5 – Pompa pannelli"
          sublabel="Wilo PWM2 diretto (20%=standby, 95%=max)"
          active={c1WiloState.running}
        >
          {c1Latch && (
            <div
              style={{ fontSize: 11, color: "var(--danger)", fontWeight: 700 }}
            >
              STOP HARD S4
            </div>
          )}
          <div
            style={{
              fontSize: 22,
              fontWeight: 700,
              color: c1WiloState.running ? "var(--ok)" : "var(--text-muted)",
            }}
          >
            {c1WiloState.label}
          </div>
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
            Duty attuale: {c1WiloState.duty}% ({c1WiloState.detail})
          </div>
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
            Richiesta manuale: {manualWiloState.duty}% ({manualWiloState.detail}
            )
          </div>
          <WiloDutyBar wiloDutyPct={c1WiloDutyPct} />
        </ActuatorCard>

        {Object.entries(RELAY_META).map(([name, meta]) => (
          <ActuatorCard
            key={name}
            label={`${meta.terminal} – ${meta.title}`}
            sublabel={meta.detail}
            active={relays[name] ?? false}
          >
            <div
              style={{
                fontSize: 22,
                fontWeight: 700,
                color:
                  (relays[name] ?? false) ? "var(--ok)" : "var(--text-muted)",
              }}
            >
              {(relays[name] ?? false) ? "ON" : "OFF"}
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Disponibilità:{" "}
              {(relayAvailable[name] ?? false) ? "OK" : "Non disponibile"}
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Gestione firmware: {meta.automation}
            </div>
            {name === "CR" && crEmerg && (
              <div
                style={{ fontSize: 11, color: "var(--warn)", fontWeight: 700 }}
              >
                Modalità emergenza/antileg attiva
              </div>
            )}
          </ActuatorCard>
        ))}
      </div>

      {zones.map((zone) => (
        <React.Fragment key={zone}>
          <SectionTitle>{ZONE_LABELS[zone]}</SectionTitle>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
              gap: 10,
            }}
          >
            {sensorsByZone[zone].map((id) => (
              <TempCard
                key={id}
                id={id}
                value={temps[id]}
                alarm={alarms[SENSOR_ALARM_MAP[id]]}
              />
            ))}
          </div>
        </React.Fragment>
      ))}

      <SectionTitle>Ingressi logici</SectionTitle>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))",
          gap: 10,
        }}
      >
        {Object.entries(INPUT_META).map(([name, meta]) => (
          <InputCard key={name} name={name} meta={meta} inputs={inputs} />
        ))}
      </div>

      <SectionTitle>Comandi manuali</SectionTitle>
      <div style={{ display: "grid", gap: 10 }}>
        <ManualModeCard
          manualMode={manualMode}
          online={online}
          busy={busyKey === "manual-mode"}
          onToggle={sendManualMode}
        />

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))",
            gap: 10,
          }}
        >
          <div
            className="card"
            style={{
              padding: "12px 14px",
              display: "flex",
              flexDirection: "column",
              gap: 8,
            }}
          >
            <div>
              <div style={{ fontWeight: 700 }}>C1 – Duty manuale Wilo PWM2</div>
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                Pilotaggio pompa pannelli su A0.5.
              </div>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Duty attuale {c1WiloState.duty}% ({c1WiloState.detail}) ·
              richiesta manuale {manualWiloState.duty}% (
              {manualWiloState.detail})
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Range utile 5-85%. Standby: 95%.
            </div>
            <div
              style={{
                display: "flex",
                gap: 8,
                flexWrap: "wrap",
                alignItems: "center",
              }}
            >
              <input
                type="number"
                min="0"
                max="95"
                step="1"
                value={pwmDraft}
                disabled={!online || !manualMode}
                onChange={(e) => setPwmDraft(e.target.value)}
                style={{
                  width: 100,
                  padding: "8px 10px",
                  borderRadius: 8,
                  border: "1px solid var(--border)",
                  background: "var(--surface-soft)",
                  color: "var(--text)",
                }}
              />
              <button
                type="button"
                className="btn btn-primary"
                disabled={!online || !manualMode || busyKey === "pwm"}
                onClick={sendPWM}
              >
                {busyKey === "pwm" ? "..." : "Applica duty"}
              </button>
              <button
                type="button"
                className="btn"
                disabled={!online || !manualMode || busyKey === "pwm"}
                onClick={() => {
                  setPwmDraft(String(WILO_STOP_DUTY_PCT));
                  postCommand(
                    "pwm",
                    "/api/acs/pwm",
                    { duty: WILO_STOP_DUTY_PCT },
                    "C1 messa in standby Wilo PWM2.",
                  );
                }}
              >
                Standby
              </button>
            </div>
          </div>

          <div
            className="card"
            style={{
              padding: "12px 14px",
              display: "flex",
              flexDirection: "column",
              gap: 8,
            }}
          >
            <div>
              <div style={{ fontWeight: 700 }}>Piscina appena riempita</div>
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                Flag operativo per la logica Block 2 piscina/riscaldamento.
              </div>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Stato flag:{" "}
              <span
                style={{
                  fontWeight: 700,
                  color: poolJustFilled ? "var(--warn)" : "var(--text-muted)",
                }}
              >
                {poolJustFilled ? "ATTIVO" : "SPENTO"}
              </span>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Nel firmware attivo questo flag entra nella logica Block 2 e puo
              avviare pompa piscina, valvola, GAS e comando ACR secondo le
              regole runtime.
            </div>
            <div
              style={{
                display: "flex",
                gap: 8,
                flexWrap: "wrap",
                alignItems: "center",
              }}
            >
              <button
                type="button"
                className="btn btn-primary"
                disabled={
                  !online || busyKey === "pool-just-filled" || poolJustFilled
                }
                onClick={() => sendPoolJustFilled(true)}
              >
                {busyKey === "pool-just-filled" ? "..." : "Segna riempita"}
              </button>
              <button
                type="button"
                className="btn"
                disabled={
                  !online || busyKey === "pool-just-filled" || !poolJustFilled
                }
                onClick={() => sendPoolJustFilled(false)}
              >
                Azzera flag
              </button>
            </div>
          </div>

          {Object.entries(RELAY_META).map(([name, meta]) => (
            <RelayControlCard
              key={name}
              name={name}
              meta={meta}
              actual={relays[name]}
              requested={manualRelays[name]}
              available={relayAvailable[name]}
              manualMode={manualMode}
              online={online}
              busy={busyKey === `relay-${name}`}
              onCommand={sendRelay}
            />
          ))}
        </div>
      </div>

      <SectionTitle>Setpoint temperature</SectionTitle>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
          gap: 10,
        }}
      >
        {Object.entries(setpointMeta).map(([key, meta]) => (
          <SetpointCard
            key={key}
            id={key}
            meta={meta}
            value={setpoints[key]}
            draft={setpointDrafts[key]}
            disabled={!online}
            busy={busyKey === `setpoint-${key}`}
            onDraftChange={updateSetpointDraft}
            onApply={sendSetpoint}
          />
        ))}
      </div>

      <SectionTitle>Eco notte gas</SectionTitle>
      <div
        className="card"
        style={{
          padding: "14px 16px",
          display: "flex",
          flexDirection: "column",
          gap: 10,
        }}
      >
        <div
          style={{
            display: "flex",
            gap: 16,
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <div>
            <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
              Profilo attivo
            </div>
            <div
              style={{
                fontSize: 16,
                fontWeight: 700,
                color: nightEco.night_active ? "var(--warn)" : "var(--ok)",
              }}
            >
              {nightEco.night_active ? "NOTTE ECO" : "GIORNO"}
            </div>
          </div>

          <div>
            <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
              Scheduler
            </div>
            <div
              style={{
                fontSize: 16,
                fontWeight: 700,
                color: nightEco.enabled ? "var(--ok)" : "var(--text-muted)",
              }}
            >
              {nightEco.enabled ? "ATTIVO" : "DISATTIVO"}
            </div>
          </div>

          <div>
            <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
              Ultima applicazione
            </div>
            <div style={{ fontSize: 15, fontWeight: 700 }}>
              {nightEco.last_applied_at
                ? formatDate(nightEco.last_applied_at)
                : "—"}
            </div>
            {nightEco.last_result && (
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                {nightEco.last_result}
              </div>
            )}
          </div>
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
            gap: 10,
            alignItems: "end",
            borderTop: "1px solid var(--border)",
            paddingTop: 10,
          }}
        >
          <label
            style={{ display: "flex", flexDirection: "column", gap: 6 }}
          >
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
              Scheduler eco notte
            </span>
            <select
              value={nightEcoDraft.enabled ? "1" : "0"}
              onChange={(e) => {
                setNightEcoDirty(true);
                setNightEcoDraft((prev) => ({
                  ...prev,
                  enabled: e.target.value === "1",
                }));
              }}
            >
              <option value="0">Disattivato</option>
              <option value="1">Attivato</option>
            </select>
          </label>

          <label
            style={{ display: "flex", flexDirection: "column", gap: 6 }}
          >
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
              Inizio notte
            </span>
            <input
              type="time"
              value={nightEcoDraft.start_hhmm}
              onChange={(e) => {
                setNightEcoDirty(true);
                setNightEcoDraft((prev) => ({
                  ...prev,
                  start_hhmm: e.target.value,
                }));
              }}
            />
          </label>

          <label
            style={{ display: "flex", flexDirection: "column", gap: 6 }}
          >
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
              Fine notte
            </span>
            <input
              type="time"
              value={nightEcoDraft.end_hhmm}
              onChange={(e) => {
                setNightEcoDirty(true);
                setNightEcoDraft((prev) => ({
                  ...prev,
                  end_hhmm: e.target.value,
                }));
              }}
            />
          </label>

          <label
            style={{ display: "flex", flexDirection: "column", gap: 6 }}
          >
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
              PDC giorno
            </span>
            <input
              type="number"
              step="0.5"
              value={nightEcoDraft.day_pdc_target_c}
              onChange={(e) => {
                setNightEcoDirty(true);
                setNightEcoDraft((prev) => ({
                  ...prev,
                  day_pdc_target_c: e.target.value,
                }));
              }}
            />
          </label>

          <label
            style={{ display: "flex", flexDirection: "column", gap: 6 }}
          >
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
              PDC notte
            </span>
            <input
              type="number"
              step="0.5"
              value={nightEcoDraft.night_pdc_target_c}
              onChange={(e) => {
                setNightEcoDirty(true);
                setNightEcoDraft((prev) => ({
                  ...prev,
                  night_pdc_target_c: e.target.value,
                }));
              }}
            />
          </label>

          <label
            style={{ display: "flex", flexDirection: "column", gap: 6 }}
          >
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
              Ricircolo giorno
            </span>
            <input
              type="number"
              step="0.5"
              value={nightEcoDraft.day_recirc_target_c}
              onChange={(e) => {
                setNightEcoDirty(true);
                setNightEcoDraft((prev) => ({
                  ...prev,
                  day_recirc_target_c: e.target.value,
                }));
              }}
            />
          </label>

          <label
            style={{ display: "flex", flexDirection: "column", gap: 6 }}
          >
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
              Ricircolo notte
            </span>
            <input
              type="number"
              step="0.5"
              value={nightEcoDraft.night_recirc_target_c}
              onChange={(e) => {
                setNightEcoDirty(true);
                setNightEcoDraft((prev) => ({
                  ...prev,
                  night_recirc_target_c: e.target.value,
                }));
              }}
            />
          </label>
        </div>

        <div
          style={{
            display: "flex",
            gap: 8,
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <button
            type="button"
            className="btn btn-primary"
            disabled={busyKey === "night-eco"}
            onClick={sendNightEcoSchedule}
          >
            {busyKey === "night-eco" ? "..." : "Salva eco notte"}
          </button>
        </div>

        <div
          style={{
            fontSize: 12,
            color: "var(--text-muted)",
            borderTop: "1px solid var(--border)",
            paddingTop: 8,
          }}
        >
          Questa logica e lato portale Itineris: di notte abbassa i setpoint
          del boiler PDC e del ricircolo per ridurre la probabilita di supporto
          gas, poi al mattino ripristina i valori giorno. Il firmware PLC non
          viene modificato.
        </div>
      </div>

      <SectionTitle>Antilegionella</SectionTitle>
      <div
        className="card"
        style={{
          padding: "14px 16px",
          display: "flex",
          flexDirection: "column",
          gap: 10,
        }}
      >
        <div
          style={{
            display: "flex",
            gap: 16,
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <div>
            <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
              Stato ciclo
            </div>
            <div
              style={{
                fontSize: 16,
                fontWeight: 700,
                color: antilegOk ? "var(--ok)" : "var(--warn)",
              }}
            >
              {antilegOk ? "COMPLETATO" : "Non eseguito"}
            </div>
            {antilegOk && antilegOkTs && (
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                {formatDate(antilegOkTs)}
              </div>
            )}
          </div>

          <div>
            <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
              Richiesta attiva
            </div>
            <div
              style={{
                fontSize: 16,
                fontWeight: 700,
                color: antilegRequest ? "var(--warn)" : "var(--text-muted)",
              }}
            >
              {antilegRequest ? "IN CORSO" : "—"}
            </div>
          </div>
        </div>

        <div
          style={{
            display: "flex",
            gap: 8,
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <button
            type="button"
            className="btn btn-primary"
            disabled={antilegPending || antilegRequest}
            onClick={() => sendAntileg(true)}
          >
            {antilegPending ? "..." : "Avvia ciclo antilegionella"}
          </button>
          {antilegRequest && (
            <button
              type="button"
              className="btn"
              disabled={antilegPending}
              onClick={() => sendAntileg(false)}
            >
              Annulla richiesta
            </button>
          )}
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
            gap: 10,
            alignItems: "end",
            borderTop: "1px solid var(--border)",
            paddingTop: 10,
          }}
        >
          <label
            style={{ display: "flex", flexDirection: "column", gap: 6 }}
          >
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
              Programmazione settimanale
            </span>
            <select
              value={antilegScheduleDraft.enabled ? "1" : "0"}
              onChange={(e) => {
                setAntilegScheduleDirty(true);
                setAntilegScheduleDraft((prev) => ({
                  ...prev,
                  enabled: e.target.value === "1",
                }));
              }}
            >
              <option value="0">Disattivata</option>
              <option value="1">Attivata</option>
            </select>
          </label>

          <label
            style={{ display: "flex", flexDirection: "column", gap: 6 }}
          >
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
              Giorno
            </span>
            <select
              value={String(antilegScheduleDraft.weekday)}
              onChange={(e) => {
                setAntilegScheduleDirty(true);
                setAntilegScheduleDraft((prev) => ({
                  ...prev,
                  weekday: Number(e.target.value),
                }));
              }}
            >
              {ANTILEG_WEEKDAYS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>

          <label
            style={{ display: "flex", flexDirection: "column", gap: 6 }}
          >
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
              Orario
            </span>
            <input
              type="time"
              value={antilegScheduleDraft.time_hhmm}
              onChange={(e) => {
                setAntilegScheduleDirty(true);
                setAntilegScheduleDraft((prev) => ({
                  ...prev,
                  time_hhmm: e.target.value,
                }));
              }}
            />
          </label>

          <div
            style={{ display: "flex", flexDirection: "column", gap: 6 }}
          >
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
              Prossimo avvio
            </span>
            <div style={{ fontSize: 15, fontWeight: 700 }}>
              {antilegSchedule.enabled && antilegSchedule.next_run_at
                ? formatDate(antilegSchedule.next_run_at)
                : "—"}
            </div>
          </div>

          <div
            style={{ display: "flex", flexDirection: "column", gap: 6 }}
          >
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
              Ultimo trigger scheduler
            </span>
            <div style={{ fontSize: 15, fontWeight: 700 }}>
              {antilegSchedule.last_trigger_at
                ? formatDate(antilegSchedule.last_trigger_at)
                : "—"}
            </div>
            {antilegSchedule.last_result && (
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                {antilegSchedule.last_result}
              </div>
            )}
          </div>
        </div>

        <div
          style={{
            display: "flex",
            gap: 8,
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <button
            type="button"
            className="btn btn-primary"
            disabled={busyKey === "antileg-schedule"}
            onClick={sendAntilegSchedule}
          >
            {busyKey === "antileg-schedule"
              ? "..."
              : "Salva programmazione"}
          </button>
        </div>

        {cmdMsg && (
          <div
            style={{
              fontSize: 13,
              color: cmdMsg.startsWith("Errore")
                ? "var(--danger)"
                : "var(--ok)",
              fontWeight: 600,
            }}
          >
            {cmdMsg}
          </div>
        )}

        <div
          style={{
            fontSize: 12,
            color: "var(--text-muted)",
            borderTop: "1px solid var(--border)",
            paddingTop: 8,
          }}
        >
          Il ciclo antilegionella usa CR in alta temperatura e puo essere
          avviato manualmente o programmato ogni settimana. In firmware prova
          prima il solare; se il boiler solare non e sufficiente usa gas e
          comando PDC ACR.
        </div>
      </div>

      <div
        style={{
          marginTop: 24,
          fontSize: 11,
          color: "var(--text-muted)",
          textAlign: "right",
        }}
      >
        Topic MQTT: <code>centralina/state</code> · Comandi:{" "}
        <code>centralina/cmd</code> · Poll: {POLL_MS / 1000}s
      </div>
    </div>
  );
}
