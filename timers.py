"""The plain-code part of a rule: how long has the answer been yes? No models in here."""
from dataclasses import dataclass

GRACE_MISSES = 1    # one stray "no" from the model doesn't restart the clock or clear an alert
RECHECK_AFTER_S = 15  # after Wonder says no, wait this long before asking it again


@dataclass
class RuleTimer:
    need: float
    since: float | None = None
    misses: int = 0
    fired: bool = False
    hold_off_until: float = 0.0
    alert_until: float = 0.0

    def step(self, yes, t):
        """Feed one check. Returns (state, seconds held). State is ok, watch, due or alert."""
        if yes:
            self.misses = 0
            if self.since is None: self.since = t
        elif self.since is None:
            return "ok", 0.0
        else:
            self.misses += 1
            if self.fired and t < self.alert_until: return "alert", round(t - self.since, 1)  # a short sound stays on screen
            if self.misses > GRACE_MISSES:
                self.since, self.misses, self.fired = None, 0, False
                return "ok", 0.0
        held = round(t - self.since, 1)
        if self.fired: return "alert", held
        if yes and held >= self.need and t >= self.hold_off_until: return "due", held
        return "watch", held

    def confirmed(self, t=0.0, hold_s=0.0):
        self.fired, self.alert_until = True, t + hold_s

    def rejected(self, t):
        self.since, self.misses, self.fired = None, 0, False
        self.hold_off_until = t + RECHECK_AFTER_S
