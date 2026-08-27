# src/core/utils/date_utils.py
from datetime import datetime, timedelta
from typing import NamedTuple, List

class TimeInterval(NamedTuple):
    start: datetime
    end: datetime

def diff_minutes(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() / 60))

def intervals_overlap(a: TimeInterval, b: TimeInterval) -> bool:
    return a.start < b.end and a.end > b.start

def subtract_intervals(source: TimeInterval, busy: List[TimeInterval]) -> List[TimeInterval]:
    remaining = [source]
    sorted_busy = sorted([b for b in busy if intervals_overlap(source, b)], key=lambda x: x.start)

    for b in sorted_busy:
        next_remaining: List[TimeInterval] = []
        for seg in remaining:
            if not intervals_overlap(seg, b):
                next_remaining.append(seg)
            else:
                if seg.start < b.start:
                    next_remaining.append(TimeInterval(start=seg.start, end=min(seg.end, b.start)))
                if seg.end > b.end:
                    next_remaining.append(TimeInterval(start=max(seg.start, b.end), end=seg.end))
        remaining = next_remaining

    return [seg for seg in remaining if diff_minutes(seg.start, seg.end) > 0]
