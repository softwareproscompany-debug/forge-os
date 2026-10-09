import { useCallback, useEffect, useMemo, useState } from "react";
import { analyticsApi, campaignApi } from "../lib/api";
import type { Enrollment } from "../lib/api";
import { dayKey, parseDayKey, toDayKey } from "../lib/format";
import { ErrorBanner, Spinner } from "../components/ui";

interface DayEvent {
  campaignName: string;
  contactId: string;
  step: number;
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export default function CalendarPage() {
  const now = new Date();
  const [year, setYear] = useState(now.getFullYear());
  const [month, setMonth] = useState(now.getMonth());
  const [scheduled, setScheduled] = useState<Map<string, DayEvent[]>>(new Map());
  const [sentByDay, setSentByDay] = useState<Map<string, number>>(new Map());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const camps = await campaignApi.list();
      const enrollLists = await Promise.all(
        camps.map(async (c) => {
          const enrolls: Enrollment[] = await campaignApi
            .enrollments(c.id)
            .catch(() => []);
          return { name: c.name, enrolls };
        }),
      );
      const sched = new Map<string, DayEvent[]>();
      for (const { name, enrolls } of enrollLists) {
        for (const en of enrolls) {
          if (!en.next_run_at || en.status !== "active") continue;
          const key = dayKey(en.next_run_at);
          const arr = sched.get(key) ?? [];
          arr.push({
            campaignName: name,
            contactId: en.contact_id,
            step: en.current_step + 1,
          });
          sched.set(key, arr);
        }
      }
      setScheduled(sched);

      const ov = await analyticsApi.overview({ days: 62 });
      const sent = new Map<string, number>();
      for (const d of ov.by_day) {
        sent.set(d.date.slice(0, 10), d.sent);
      }
      setSentByDay(sent);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const cells = useMemo(() => {
    const first = new Date(year, month, 1);
    const startOffset = first.getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const list: (string | null)[] = [];
    for (let i = 0; i < startOffset; i++) list.push(null);
    for (let d = 1; d <= daysInMonth; d++) {
      list.push(toDayKey(new Date(year, month, d)));
    }
    return list;
  }, [year, month]);

  const monthLabel = new Date(year, month, 1).toLocaleDateString("en-US", {
    month: "long",
    year: "numeric",
  });

  const shiftMonth = (delta: number) => {
    const d = new Date(year, month + delta, 1);
    setYear(d.getFullYear());
    setMonth(d.getMonth());
  };

  const todayKey = toDayKey(new Date());

  if (loading) return <Spinner label="Loading calendar…" />;

  return (
    <div>
      <div className="page-head">
        <h1>Send calendar</h1>
        <div className="row-actions">
          <button type="button" className="btn btn-sm" onClick={() => shiftMonth(-1)}>
            ← Prev
          </button>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => {
              const t = new Date();
              setYear(t.getFullYear());
              setMonth(t.getMonth());
            }}
          >
            Today
          </button>
          <button type="button" className="btn btn-sm" onClick={() => shiftMonth(1)}>
            Next →
          </button>
        </div>
      </div>

      <ErrorBanner error={error} onRetry={load} />

      <div className="card">
        <div className="card-head">
          <h2>{monthLabel}</h2>
          <div className="legend">
            <span className="legend-item">
              <span className="dot dot-blue" /> scheduled
            </span>
            <span className="legend-item">
              <span className="dot dot-green" /> sent
            </span>
          </div>
        </div>
        <div className="calendar-grid" role="grid" aria-label={monthLabel}>
          {WEEKDAYS.map((w) => (
            <div key={w} className="cal-weekday">
              {w}
            </div>
          ))}
          {cells.map((key, i) => {
            if (!key) return <div key={`blank-${i}`} className="cal-day cal-blank" />;
            const events = scheduled.get(key) ?? [];
            const sent = sentByDay.get(key) ?? 0;
            const isToday = key === todayKey;
            return (
              <div
                key={key}
                className={`cal-day${isToday ? " cal-today" : ""}`}
                role="gridcell"
                aria-label={parseDayKey(key).toDateString()}
              >
                <div className="cal-date">{parseDayKey(key).getDate()}</div>
                <div className="cal-events">
                  {events.length > 0 && (
                    <div className="cal-chip cal-scheduled" title={events.map((e) => `${e.campaignName} — step ${e.step}`).join("\n")}>
                      <span className="dot dot-blue" />
                      {events.length} scheduled
                    </div>
                  )}
                  {sent > 0 && (
                    <div className="cal-chip" title={`${sent} sends on ${key}`}>
                      <span className="dot dot-green" />
                      {sent} sent
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
        <p className="muted" style={{ marginTop: 12 }}>
          Scheduled items come from active campaign enrollments’ next run time;
          sent counts come from analytics. The API has no per-send schedule
          listing, so future queued sends aren’t shown individually.
        </p>
      </div>
    </div>
  );
}
