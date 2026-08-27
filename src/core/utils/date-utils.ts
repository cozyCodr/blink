// src/core/utils/date-utils.ts

export interface TimeInterval {
  start: Date;
  end: Date;
}

export function parseISO(dateStr: string): Date {
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) {
    throw new Error(`Invalid ISO date string: ${dateStr}`);
  }
  return d;
}

export function toISODateOnly(d: Date): string {
  return d.toISOString().slice(0, 10);
}

export function addMinutes(d: Date, minutes: number): Date {
  return new Date(d.getTime() + minutes * 60_000);
}

export function diffMinutes(start: Date, end: Date): number {
  return Math.max(0, Math.round((end.getTime() - start.getTime()) / 60_000));
}

export function intervalsOverlap(a: TimeInterval, b: TimeInterval): boolean {
  return a.start < b.end && a.end > b.start;
}

export function subtractIntervals(source: TimeInterval, busy: TimeInterval[]): TimeInterval[] {
  let remaining: TimeInterval[] = [{ ...source }];

  // Sort busy intervals by start time
  const sortedBusy = [...busy]
    .filter(b => intervalsOverlap(source, b))
    .sort((x, y) => x.start.getTime() - y.start.getTime());

  for (const b of sortedBusy) {
    const nextRemaining: TimeInterval[] = [];
    for (const seg of remaining) {
      if (!intervalsOverlap(seg, b)) {
        nextRemaining.push(seg);
      } else {
        // Left leftover segment
        if (seg.start < b.start) {
          nextRemaining.push({ start: seg.start, end: new Date(Math.min(seg.end.getTime(), b.start.getTime())) });
        }
        // Right leftover segment
        if (seg.end > b.end) {
          nextRemaining.push({ start: new Date(Math.max(seg.start.getTime(), b.end.getTime())), end: seg.end });
        }
      }
    }
    remaining = nextRemaining;
  }

  return remaining.filter(seg => diffMinutes(seg.start, seg.end) > 0);
}
