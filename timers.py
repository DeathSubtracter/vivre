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
    fired_at: float = 0.0
    rearm_s: float = 0.0   # 0 means a rule can't fire again until it has fully cleared
    quiet: bool = False    # has the sound stopped since the last alert?

    def step(self, yes, t):
        """Feed one check. Returns (state, seconds held). State is ok, watch, due or alert."""
        if yes:
            ready = self.can_refire(t)
            self.misses = 0
            if self.since is None: self.since = t
            if ready: return "due", round(t - self.since, 1)  # a new incident while the last one is still on screen
        elif self.since is None:
            return "ok", 0.0
        else:
            self.misses += 1
            if self.fired and t < self.alert_until:  # a short sound stays on screen
                self.quiet = True
                return "alert", round(t - self.since, 1)
            if self.misses > GRACE_MISSES:
                self.since, self.misses, self.fired = None, 0, False
                return "ok", 0.0
        held = round(t - self.since, 1)
        if self.fired: return "alert", held
        if yes and held >= self.need and t >= self.hold_off_until: return "due", held
        return "watch", held

    def confirmed(self, t=0.0, hold_s=0.0, rearm_s=0.0):
        self.fired, self.alert_until, self.fired_at, self.rearm_s, self.quiet = True, t + hold_s, t, rearm_s, False

    def can_refire(self, t):
        return bool(self.fired and self.rearm_s and self.quiet and t - self.fired_at >= self.rearm_s)

    def rejected(self, t):
        self.since, self.misses, self.fired = None, 0, False
        self.hold_off_until = t + RECHECK_AFTER_S
