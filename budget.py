"""Spending caps. When one trips, live model calls stop and the app switches to the recorded sample."""
import json, os, threading, time


class Budget:
    def __init__(self, per_session, per_ip_day, global_total, path=None):
        self.per_session, self.per_ip_day, self.global_total = per_session, per_ip_day, global_total
        self.path, self.lock, self.ip, self.day = path, threading.Lock(), {}, ""
        self.total = 0.0
        if path and os.path.exists(path):  # survives a restart; a redeploy may clear it
            try:
                with open(path) as f: self.total = float(json.load(f)["total"])
            except (ValueError, KeyError, OSError):
                self.total = global_total  # unreadable record: stop rather than risk overspending

    def _roll_day(self):
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self.day: self.day, self.ip = today, {}

    def charge(self, ip, usd):
        with self.lock:
            self._roll_day()
            self.ip[ip] = self.ip.get(ip, 0.0) + usd
            self.total += usd
            if self.path:
                try:
                    with open(self.path, "w") as f: json.dump({"total": self.total}, f)
                except OSError as e:
                    print(f"Couldn't save the spend total: {type(e).__name__}")

    def blocked(self, ip, session_spent=0.0):
        """The reason live calls must stop, or None."""
        with self.lock:
            self._roll_day()
            if self.total >= self.global_total: return "The live budget for this demo has been used up."
            if self.ip.get(ip, 0.0) >= self.per_ip_day: return "You've reached today's live limit."
            if session_spent >= self.per_session: return "This session reached its live budget."
        return None
