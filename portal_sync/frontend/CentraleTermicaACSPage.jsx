import React, { useCallback, useEffect, useState } from 'react';
import { getToken } from './auth';

const POLL_MS = 5000;

const SENSOR_META = {
  S1: { label: 'Pannelli solari', icon: '☀️', zone: 'solare' },
  S2: { label: 'Boiler solare (centro)', icon: '🔵', zone: 'solare' },
  S3: { label: 'Boiler solare (alto)', icon: '🔴', zone: 'solare' },
  S4: { label: 'Boiler PDC (alto)', icon: '🔴', zone: 'pdc' },
  S5: { label: 'Boiler PDC (basso)', icon: '🔵', zone: 'pdc' },
  S6: { label: 'Collettore ricircolo (in)', icon: '🔵', zone: 'recirc' },
  S7: { label: 'Collettore ricircolo (out)', icon: '🔴', zone: 'recirc' },
};

const ZONE_LABELS = {
  solare: 'Impianto solare',
  pdc: 'Pompa di calore',
  recirc: 'Ricircolo collettore',
};

const ALARM_LABELS = {
  ALARM_SENSORS_PANELS: 'Sonde pannelli (S1/S2/S3)',
  ALARM_SENSORS_C2: 'Sonde C2 (S2/S3/S4/S5)',
  ALARM_SENSORS_CR: 'Sonde CR (S6/S7)',
  ALARM_S4_INVALID: 'Sonda S4 (critica stop hard)',
};

const RELAY_META = {
  C2: { label: 'C2', title: 'Pompa trasferimento', detail: 'Solare → PDC' },
  PISCINA_PUMP: { label: 'Q0.1', title: 'Pompa piscina', detail: 'Richiesta piscina' },
  HEAT_PUMP: { label: 'Q0.2', title: 'Pompa aiuto riscaldamento', detail: 'Supporto riscaldamento' },
  CR: { label: 'Q0.3', title: 'Pompa ricircolo', detail: 'Collettore ACS' },
  VALVE: { label: 'Q0.4', title: 'Valvola EVIE', detail: 'Valvola motorizzata' },
  GAS_ENABLE: { label: 'Q0.6', title: 'GAS', detail: 'Abilitazione gas' },
  PDC_CMD_START_ACR: { label: 'Q0.7', title: 'Avvio lavoro ACR', detail: 'Comando PDC' },
};

const SENSOR_ALARM_MAP = {
  S1: 'ALARM_SENSORS_PANELS',
  S2: 'ALARM_SENSORS_PANELS',
  S3: 'ALARM_SENSORS_PANELS',
  S4: 'ALARM_S4_INVALID',
  S5: 'ALARM_SENSORS_C2',
  S6: 'ALARM_SENSORS_CR',
  S7: 'ALARM_SENSORS_CR',
};

const WILO_STOP_DUTY_PCT = 95;
const WILO_MIN_RUN_DUTY_PCT = 85;
const WILO_MAX_RUN_DUTY_PCT = 5;

function clampPct(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return 0;
  return Math.max(0, Math.min(100, Math.round(num)));
}

function getWiloSpeedPct(wiloDutyPct) {
  const duty = clampPct(wiloDutyPct);
  if (duty <= 0 || duty >= WILO_STOP_DUTY_PCT) return 0;

  const boundedDuty = Math.max(WILO_MAX_RUN_DUTY_PCT, Math.min(WILO_MIN_RUN_DUTY_PCT, duty));
  const speedPct = 1 + ((WILO_MIN_RUN_DUTY_PCT - boundedDuty) * 99) / (WILO_MIN_RUN_DUTY_PCT - WILO_MAX_RUN_DUTY_PCT);
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
      label: 'OFF',
      detail: 'uscita non inizializzata',
    };
  }

  if (duty >= WILO_STOP_DUTY_PCT) {
    return {
      duty,
      speedPct: 0,
      running: false,
      label: 'STOP',
      detail: 'standby Wilo PWM2',
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

function fmt(val, unit = '°C') {
  if (val === null || val === undefined) return '—';
  return `${Number(val).toFixed(1)} ${unit}`;
}

function formatAgo(ts) {
  if (!ts) return '—';
  const diff = Math.max(0, Date.now() / 1000 - ts);
  if (diff < 60) return `${Math.floor(diff)}s fa`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m fa`;
  return `${Math.floor(diff / 3600)}h fa`;
}

function formatDate(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString('it-IT');
}

function TempCard({ id, value, alarm }) {
  const meta = SENSOR_META[id] || {};
  const missing = value === null || value === undefined;
  const color = missing || alarm
    ? 'var(--danger)'
    : value > 80
      ? '#f44336'
      : value > 60
        ? 'var(--warn)'
        : value > 40
          ? 'var(--ok)'
          : '#64b5f6';

  return (
    <div className="card" style={{
      borderLeft: `4px solid ${color}`,
      display: 'flex',
      flexDirection: 'column',
      gap: 4,
      padding: '10px 14px',
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>
          {meta.icon} {id} – {meta.label}
        </span>
        {(missing || alarm) && (
          <span style={{ fontSize: 11, color: 'var(--danger)', fontWeight: 600 }}>
            {alarm ? '⚠ ALLARME' : 'INVALID'}
          </span>
        )}
      </div>
      <div style={{ fontSize: 28, fontWeight: 700, color }}>
        {missing ? '— °C' : fmt(value)}
      </div>
    </div>
  );
}

function ActuatorCard({ label, sublabel, active, children }) {
  const color = active ? 'var(--ok)' : 'var(--text-muted)';
  return (
    <div className="card" style={{
      borderLeft: `4px solid ${color}`,
      padding: '10px 14px',
      display: 'flex',
      flexDirection: 'column',
      gap: 4,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <span style={{ fontWeight: 600 }}>{label}</span>
        <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>{sublabel}</span>
      </div>
      {children}
    </div>
  );
}

function AlarmRow({ id, active }) {
  if (!active) return null;
  return (
    <div style={{
      background: 'color-mix(in oklab, var(--danger) 18%, var(--surface))',
      border: '1px solid var(--danger)',
      borderRadius: 8,
      padding: '8px 12px',
      fontSize: 13,
      color: 'var(--danger)',
      fontWeight: 600,
      display: 'flex',
      alignItems: 'center',
      gap: 8,
    }}>
      ⚠ {ALARM_LABELS[id] || id}
    </div>
  );
}

function SectionTitle({ children }) {
  return (
    <h3 style={{
      margin: '24px 0 10px',
      fontSize: 14,
      fontWeight: 700,
      textTransform: 'uppercase',
      letterSpacing: '0.08em',
      color: 'var(--text-muted)',
      borderBottom: '1px solid var(--border)',
      paddingBottom: 6,
    }}>
      {children}
    </h3>
  );
}

function WiloDutyBar({ wiloDutyPct }) {
  const wiloState = getWiloState(wiloDutyPct);
  return (
    <div style={{ marginTop: 4 }}>
      <div style={{
        height: 10,
        background: 'var(--surface-soft)',
        borderRadius: 5,
        overflow: 'hidden',
        border: '1px solid var(--border)',
      }}>
        <div style={{
          width: `${wiloState.speedPct}%`,
          height: '100%',
          background: wiloState.running ? 'var(--ok)' : 'var(--border)',
          transition: 'width 0.4s ease',
        }} />
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
        {wiloState.running
          ? `Duty Wilo PWM2: ${wiloState.duty}% · velocita stimata ${wiloState.speedPct}%`
          : `Duty Wilo PWM2: ${wiloState.duty}% · ${wiloState.detail}`}
      </div>
    </div>
  );
}

function ManualModeCard({ manualMode, online, busy, onToggle }) {
  return (
    <div className="card" style={{ padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, flexWrap: 'wrap' }}>
        <div>
          <div style={{ fontWeight: 700 }}>Modalità manuale</div>
          <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
            Quando è spenta, il firmware forza le uscite in safe-state finché la logica automatica non è confermata.
          </div>
        </div>
        <div style={{
          fontSize: 12,
          fontWeight: 700,
          color: manualMode ? 'var(--warn)' : 'var(--text-muted)',
        }}>
          {manualMode ? 'MANUALE ATTIVA' : 'SAFE-STATE'}
        </div>
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!online || busy || manualMode}
          onClick={() => onToggle(true)}
        >
          {busy && !manualMode ? '...' : 'Abilita manuale'}
        </button>
        <button
          type="button"
          className="btn"
          disabled={!online || busy || !manualMode}
          onClick={() => onToggle(false)}
        >
          {busy && manualMode ? '...' : 'Disabilita manuale'}
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
    <div className="card" style={{
      borderLeft: `4px solid ${active ? 'var(--ok)' : 'var(--text-muted)'}`,
      padding: '12px 14px',
      display: 'flex',
      flexDirection: 'column',
      gap: 8,
    }}>
      <div>
        <div style={{ fontWeight: 700 }}>{meta.label} – {meta.title}</div>
        <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>{meta.detail}</div>
      </div>
      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', fontSize: 12 }}>
        <div>
          <div style={{ color: 'var(--text-muted)' }}>Stato uscita</div>
          <div style={{ fontWeight: 700, color: active ? 'var(--ok)' : 'var(--text-muted)' }}>
            {active ? 'ON' : 'OFF'}
          </div>
        </div>
        <div>
          <div style={{ color: 'var(--text-muted)' }}>Richiesta manuale</div>
          <div style={{ fontWeight: 700 }}>{requested ? 'ON' : 'OFF'}</div>
        </div>
        <div>
          <div style={{ color: 'var(--text-muted)' }}>Disponibilità</div>
          <div style={{ fontWeight: 700, color: available ? 'var(--ok)' : 'var(--danger)' }}>
            {available ? 'OK' : 'Non disponibile'}
          </div>
        </div>
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!online || !manualMode || !available || busy}
          onClick={() => onCommand(name, true)}
        >
          {busy ? '...' : 'ON'}
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

function SetpointCard({ id, meta, value, draft, disabled, busy, onDraftChange, onApply }) {
  return (
    <div className="card" style={{ padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div>
        <div style={{ fontWeight: 700 }}>{meta?.label || id}</div>
        <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
          Attuale: {fmt(value)} · Range {meta?.min ?? '—'} / {meta?.max ?? '—'}
        </div>
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <input
          type="number"
          min={meta?.min}
          max={meta?.max}
          step={meta?.step ?? 0.5}
          value={draft ?? ''}
          disabled={disabled}
          onChange={(e) => onDraftChange(id, e.target.value)}
          style={{
            width: 110,
            padding: '8px 10px',
            borderRadius: 8,
            border: '1px solid var(--border)',
            background: 'var(--surface-soft)',
            color: 'var(--text)',
          }}
        />
        <button
          type="button"
          className="btn btn-primary"
          disabled={disabled || busy}
          onClick={() => onApply(id)}
        >
          {busy ? '...' : 'Applica'}
        </button>
      </div>
    </div>
  );
}

export default function CentraleTermicaACSPage() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [cmdMsg, setCmdMsg] = useState('');
  const [busyKey, setBusyKey] = useState('');
  const [antilegPending, setAntilegPending] = useState(false);
  const [pwmDraft, setPwmDraft] = useState('');
  const [setpointDrafts, setSetpointDrafts] = useState({});

  const fetchState = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const res = await fetch('/api/acs/state', {
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      if (!res.ok) {
        const text = await res.text().catch(() => '');
        setError(text || `Errore ${res.status}`);
        return;
      }
      const next = await res.json();
      setData(next);
      setError('');
    } catch (_err) {
      if (!silent) setError('Errore di rete');
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
    if (!data) return;
    setPwmDraft((prev) => (prev === ''
      ? String(data.manual_c1_wilo_duty_pct ?? data.manual_pwm_duty ?? data.c1_wilo_duty_pct ?? data.c1_duty ?? 0)
      : prev));
    setSetpointDrafts((prev) => {
      const next = { ...prev };
      let changed = false;
      Object.entries(data.setpoints ?? {}).forEach(([key, value]) => {
        if (!(key in next)) {
          next[key] = String(value ?? '');
          changed = true;
        }
      });
      return changed ? next : prev;
    });
  }, [data]);

  const postCommand = useCallback(async (key, url, body, successMsg) => {
    setBusyKey(key);
    setCmdMsg('');
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${getToken()}`,
          'Content-Type': 'application/json',
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
      setCmdMsg('Errore di rete nel comando.');
      return false;
    } finally {
      setBusyKey('');
    }
  }, [fetchState]);

  const sendAntileg = async (value) => {
    setAntilegPending(true);
    setCmdMsg('');
    try {
      const res = await fetch('/api/acs/antileg', {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${getToken()}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ request: value }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) setCmdMsg(`Errore: ${payload.detail || res.status}`);
      else setCmdMsg(value ? 'Ciclo antilegionella avviato.' : 'Richiesta antilegionella annullata.');
      fetchState(true);
    } catch (_err) {
      setCmdMsg('Errore di rete nel comando.');
    } finally {
      setAntilegPending(false);
    }
  };

  const sendManualMode = (enabled) => postCommand(
    'manual-mode',
    '/api/acs/manual-mode',
    { enabled },
    enabled ? 'Modalità manuale abilitata.' : 'Modalità manuale disabilitata.',
  );

  const sendRelay = (name, value) => postCommand(
    `relay-${name}`,
    '/api/acs/relay',
    { name, state: value },
    `${name} impostato su ${value ? 'ON' : 'OFF'}.`,
  );

  const sendPWM = async () => {
    const wiloDutyPct = Number(pwmDraft);
    if (!Number.isFinite(wiloDutyPct) || wiloDutyPct < 0 || wiloDutyPct > 100) {
      setCmdMsg('Errore: duty Wilo PWM2 non valido (0-100).');
      return;
    }
    const ok = await postCommand(
      'pwm',
      '/api/acs/pwm',
      { duty: Math.round(wiloDutyPct) },
      `Duty Wilo PWM2 C1 impostato a ${Math.round(wiloDutyPct)}%.`,
    );
    if (ok) setPwmDraft(String(Math.round(wiloDutyPct)));
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
      '/api/acs/setpoint',
      { key, value },
      `Setpoint ${key} aggiornato.`,
    );
    if (ok) {
      setSetpointDrafts((prev) => ({ ...prev, [key]: String(value) }));
    }
  };

  const sendPoolJustFilled = (enabled) => postCommand(
    'pool-just-filled',
    '/api/acs/pool-just-filled',
    { enabled },
    enabled ? 'Flag piscina appena riempita attivato.' : 'Flag piscina appena riempita azzerato.',
  );

  if (loading && !data) {
    return <div style={{ padding: 24 }}>Caricamento...</div>;
  }

  if (error && !data) {
    return (
      <div style={{ padding: 24 }}>
        <h2>Centrale termica ACS</h2>
        <div style={{ color: 'var(--danger)' }}>{error}</div>
      </div>
    );
  }

  const temps = data?.temps ?? {};
  const alarms = data?.alarms ?? {};
  const relays = data?.relays ?? {};
  const relayAvailable = data?.relay_available ?? {};
  const manualRelays = data?.manual_relays ?? {};
  const setpoints = data?.setpoints ?? {};
  const setpointMeta = data?.setpoint_meta ?? {};
  const c1WiloDutyPct = data?.c1_wilo_duty_pct ?? data?.c1_duty ?? 0;
  const c1Latch = data?.c1_latch ?? false;
  const crEmerg = data?.cr_emerg ?? false;
  const manualMode = data?.manual_mode ?? false;
  const manualWiloDutyPct = data?.manual_c1_wilo_duty_pct ?? data?.manual_pwm_duty ?? 0;
  const c1WiloState = getWiloState(c1WiloDutyPct);
  const poolJustFilled = data?.pool_just_filled ?? false;
  const online = data?.online ?? false;
  const antilegOk = data?.antileg_ok ?? false;
  const antilegOkTs = data?.antileg_ok_ts ?? null;
  const antilegRequest = data?.antileg_request ?? false;
  const receivedAt = data?.received_at ?? null;
  const hasAlarm = Object.values(alarms).some(Boolean);
  const zones = ['solare', 'pdc', 'recirc'];
  const sensorsByZone = zones.reduce((acc, zone) => {
    acc[zone] = Object.entries(SENSOR_META).filter(([, meta]) => meta.zone === zone).map(([id]) => id);
    return acc;
  }, {});

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 8, marginBottom: 4 }}>
        <h2 style={{ margin: 0 }}>Centrale termica ACS</h2>
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 4 }}>
          <span style={{
            fontSize: 12,
            fontWeight: 700,
            color: online ? 'var(--ok)' : 'var(--danger)',
            background: online
              ? 'color-mix(in oklab, var(--ok) 15%, var(--surface))'
              : 'color-mix(in oklab, var(--danger) 15%, var(--surface))',
            border: `1px solid ${online ? 'var(--ok)' : 'var(--danger)'}`,
            borderRadius: 20,
            padding: '3px 10px',
          }}>
            {online ? '● ONLINE' : '● OFFLINE'}
          </span>
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            Ultimo dato: {formatAgo(receivedAt)}
          </span>
        </div>
      </div>

      <p style={{ margin: '4px 0 0', color: 'var(--text-muted)', fontSize: 13 }}>
        Monitoraggio e comando manuale della centrale termica ACS. Aggiornamento ogni {POLL_MS / 1000}s.
      </p>

      {error && <div style={{ marginTop: 8, color: 'var(--danger)', fontSize: 13 }}>{error}</div>}

      {hasAlarm && (
        <>
          <SectionTitle>Allarmi sensori</SectionTitle>
          <div style={{ display: 'grid', gap: 6 }}>
            {Object.entries(alarms).map(([id, active]) => (
              <AlarmRow key={id} id={id} active={active} />
            ))}
          </div>
        </>
      )}

      <SectionTitle>Attuatori</SectionTitle>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 10 }}>
        <ActuatorCard label="C1 – Pompa pannelli" sublabel="Wilo PWM2 invertito (95%=stop, 5%=max)" active={c1WiloState.running}>
          {c1Latch && (
            <div style={{ fontSize: 11, color: 'var(--danger)', fontWeight: 700 }}>
              STOP HARD S4
            </div>
          )}
          <div style={{ fontSize: 22, fontWeight: 700, color: c1WiloState.running ? 'var(--ok)' : 'var(--text-muted)' }}>
            {c1WiloState.label}
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
            Duty attuale: {c1WiloState.duty}% ({c1WiloState.detail})
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
            Richiesta manuale: {manualWiloDutyPct}%
          </div>
          <WiloDutyBar wiloDutyPct={c1WiloDutyPct} />
        </ActuatorCard>

        {Object.entries(RELAY_META).map(([name, meta]) => (
          <ActuatorCard key={name} label={`${meta.label} – ${meta.title}`} sublabel={meta.detail} active={relays[name] ?? false}>
            <div style={{ fontSize: 22, fontWeight: 700, color: (relays[name] ?? false) ? 'var(--ok)' : 'var(--text-muted)' }}>
              {(relays[name] ?? false) ? 'ON' : 'OFF'}
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
              Disponibilità: {(relayAvailable[name] ?? false) ? 'OK' : 'Non disponibile'}
            </div>
            {name === 'CR' && crEmerg && (
              <div style={{ fontSize: 11, color: 'var(--warn)', fontWeight: 700 }}>
                Modalità emergenza/antileg attiva
              </div>
            )}
          </ActuatorCard>
        ))}
      </div>

      {zones.map((zone) => (
        <React.Fragment key={zone}>
          <SectionTitle>{ZONE_LABELS[zone]}</SectionTitle>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 10 }}>
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

      <SectionTitle>Comandi manuali</SectionTitle>
      <div style={{ display: 'grid', gap: 10 }}>
        <ManualModeCard
          manualMode={manualMode}
          online={online}
          busy={busyKey === 'manual-mode'}
          onToggle={sendManualMode}
        />

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: 10 }}>
          <div className="card" style={{ padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div>
              <div style={{ fontWeight: 700 }}>C1 – Duty manuale Wilo PWM2</div>
              <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>Pilotaggio pompa pannelli su Q0.5 (switch B1 = ON)</div>
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
              Duty attuale {c1WiloState.duty}% ({c1WiloState.detail}) · richiesta manuale {manualWiloDutyPct}%
            </div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
              <input
                type="number"
                min="0"
                max="100"
                step="1"
                value={pwmDraft}
                disabled={!online || !manualMode}
                onChange={(e) => setPwmDraft(e.target.value)}
                style={{
                  width: 100,
                  padding: '8px 10px',
                  borderRadius: 8,
                  border: '1px solid var(--border)',
                  background: 'var(--surface-soft)',
                  color: 'var(--text)',
                }}
              />
              <button
                type="button"
                className="btn btn-primary"
                disabled={!online || !manualMode || busyKey === 'pwm'}
                onClick={sendPWM}
              >
                {busyKey === 'pwm' ? '...' : 'Applica duty'}
              </button>
              <button
                type="button"
                className="btn"
                disabled={!online || !manualMode || busyKey === 'pwm'}
                onClick={() => {
                  setPwmDraft('0');
                  postCommand('pwm', '/api/acs/pwm', { duty: 0 }, 'Duty Wilo PWM2 C1 disattivato.');
                }}
              >
                Off
              </button>
            </div>
          </div>

          <div className="card" style={{ padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div>
              <div style={{ fontWeight: 700 }}>Piscina appena riempita</div>
              <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                Flag operativo per la logica Block 2 piscina/riscaldamento.
              </div>
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
              Stato flag: <span style={{ fontWeight: 700, color: poolJustFilled ? 'var(--warn)' : 'var(--text-muted)' }}>
                {poolJustFilled ? 'ATTIVO' : 'SPENTO'}
              </span>
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
              Effetto operativo solo se il Block 2 e integrato nel firmware attivo.
            </div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
              <button
                type="button"
                className="btn btn-primary"
                disabled={!online || busyKey === 'pool-just-filled' || poolJustFilled}
                onClick={() => sendPoolJustFilled(true)}
              >
                {busyKey === 'pool-just-filled' ? '...' : 'Segna riempita'}
              </button>
              <button
                type="button"
                className="btn"
                disabled={!online || busyKey === 'pool-just-filled' || !poolJustFilled}
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
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 10 }}>
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

      <SectionTitle>Antilegionella</SectionTitle>
      <div className="card" style={{ padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'center' }}>
          <div>
            <div style={{ fontSize: 13, color: 'var(--text-muted)' }}>Stato ciclo</div>
            <div style={{
              fontSize: 16,
              fontWeight: 700,
              color: antilegOk ? 'var(--ok)' : 'var(--warn)',
            }}>
              {antilegOk ? 'COMPLETATO' : 'Non eseguito'}
            </div>
            {antilegOk && antilegOkTs && (
              <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                {formatDate(antilegOkTs)}
              </div>
            )}
          </div>

          <div>
            <div style={{ fontSize: 13, color: 'var(--text-muted)' }}>Richiesta attiva</div>
            <div style={{
              fontSize: 16,
              fontWeight: 700,
              color: antilegRequest ? 'var(--warn)' : 'var(--text-muted)',
            }}>
              {antilegRequest ? 'IN CORSO' : '—'}
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <button
            type="button"
            className="btn btn-primary"
            disabled={antilegPending || antilegRequest}
            onClick={() => sendAntileg(true)}
          >
            {antilegPending ? '...' : 'Avvia ciclo antilegionella'}
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

        {cmdMsg && (
          <div style={{
            fontSize: 13,
            color: cmdMsg.startsWith('Errore') ? 'var(--danger)' : 'var(--ok)',
            fontWeight: 600,
          }}>
            {cmdMsg}
          </div>
        )}

        <div style={{ fontSize: 12, color: 'var(--text-muted)', borderTop: '1px solid var(--border)', paddingTop: 8 }}>
          Il ciclo antilegionella usa ancora la logica esistente. I comandi manuali pompe e setpoint sono separati da quella logica.
        </div>
      </div>

      <div style={{ marginTop: 24, fontSize: 11, color: 'var(--text-muted)', textAlign: 'right' }}>
        Topic MQTT: <code>centralina/state</code> · Comandi: <code>centralina/cmd</code> · Poll: {POLL_MS / 1000}s
      </div>
    </div>
  );
}
