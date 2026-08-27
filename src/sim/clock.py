# src/sim/clock.py
from datetime import datetime, timedelta
from typing import Optional

class VirtualClock:
    """Injectable virtual clock for running simulations across weeks in seconds."""
    
    def __init__(self, start_time: datetime):
        self._current_time = start_time

    def now(self) -> datetime:
        return self._current_time

    def advance(self, minutes: int = 0, hours: int = 0, days: int = 0) -> datetime:
        delta = timedelta(minutes=minutes, hours=hours, days=days)
        self._current_time += delta
        return self._current_time

    def set_time(self, new_time: datetime):
        self._current_time = new_time
