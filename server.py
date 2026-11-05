"""Vivre (live): describe what to watch for in a sentence; it watches a camera feed.

Start:  each sentence -> (Spark) validated rule spec.
Check:  one camera frame -> (SAM 3.1, in the background) object counts -> (Spark) yes/no per rule
        -> plain-code timers -> if a rule comes due, (Wonder) confirms and explains.
Cheap model watches all the time; the strong model only speaks up when it matters.
Every model call happens here, so the key never reaches the browser.
"""
import json, os, threading, time, uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import meta
from budget import Budget
from ratelimit import RateLimiter
from timers import RuleTimer


def env_f(name, default): return float(os.getenv(name, default))


PER_SESSION_USD = env_f("PER_SESSION_USD", 0.75)
PER_IP_USD_DAY = env_f("PER_IP_USD_DAY", 2)
GLOBAL_USD_TOTAL = env_f("GLOBAL_USD_TOTAL", 60)
MAX_SESSION_MIN = env_f("MAX_SESSION_MIN", 4)
MAX_CONCURRENT = int(env_f("MAX_CONCURRENT", 4))
SAMPLE_ONLY = os.getenv("SAMPLE_ONLY", "0") == "1"
TRUST_PROXY = os.getenv("TRUST_PROXY", "0") == "1"  # set on the host, where a proxy sets X-Real-IP

MAX_RULES, MAX_RULE_CHARS, MAX_FRAME_BYTES, MAX_BODY_BYTES = 3, 200, 300_000, 1_000_000
MAX_CLIP_BYTES, MAX_SPOKEN_BYTES = 200_000, 600_000  # about 6 s of room sound, about 18 s of speech, as 16 kHz WAV
HEARD_MIN = float(os.getenv("HEARD_MIN", "0.25"))  # how sure the sound recognizer must be
# Real claps often score as "Hands", so some sounds also count under related labels, each with its own bar.
SOUND_ALSO = {"Clapping": {"Hands": 0.5, "Applause": 0.25}, "Baby cry, infant cry": {"Crying, sobbing": 0.4},
              "Crying, sobbing": {"Baby cry, infant cry": 0.25}, "Smoke detector, smoke alarm": {"Fire alarm": 0.25, "Alarm": 0.4},
              "Fire alarm": {"Smoke detector, smoke alarm": 0.25, "Alarm": 0.4}}
SOUND_ALERT_HOLD_S = 30  # a clap is over in a moment, so its alert stays up long enough to be seen
FRESH_S = 12             # SAM's counts and boxes older than this don't decide anything
MIN_CHECK_S = 2.9        # no session checks faster than every 3 seconds
STILL_DIFF, STILL_MAX_S = 0.015, 12  # still frames reuse the last answer, up to this long
ACTIVE_S = 20            # a session with no check for this long doesn't count as running

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
if os.getenv("RAILWAY_ENVIRONMENT_NAME"):  # on the host only, so tests stay offline
    threading.Thread(target=meta.log_visible_models, daemon=True).start()
budget = Budget(PER_SESSION_USD, PER_IP_USD_DAY, GLOBAL_USD_TOTAL, os.getenv("SPEND_FILE", "/tmp/vivre_spend.json"))
limiter = RateLimiter()
sessions, sessions_lock = {}, threading.Lock()
sam_pool = ThreadPoolExecutor(max_workers=8)
compile_pool = ThreadPoolExecutor(max_workers=6)


CSP = ("default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data: blob:; media-src 'self' blob:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Requests must come from Vivre's own page."}, 403)
        try:
            size = int(request.headers.get("content-length", ""))
        except ValueError:
            return JSONResponse({"detail": "Request size is missing."}, 411)
        if size > MAX_BODY_BYTES:
            return JSONResponse({"detail": "That upload is too large."}, 413)
    r = await call_next(request)
    r.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                      "Content-Security-Policy": CSP, "Permissions-Policy": "camera=(self), microphone=(self)"})
    return r


_proxy_logged = []


def client_ip(request):
    if TRUST_PROXY and not _proxy_logged:  # once, and without the address: is the visitor limit really per visitor?
        _proxy_logged.append(1); print(f"Visitor address header present: {bool(request.headers.get('x-real-ip'))}")
    if TRUST_PROXY and (real := request.headers.get("x-real-ip")): return real.strip()
    return request.client.host if request.client else "unknown"


def validate_spec(o, sentence):
    """Model output is untrusted too. Returns a clean spec or None."""
    if not isinstance(o, dict): return None
    q = o.get("question")
    if not isinstance(q, str) or not 5 <= len(q.strip()) <= 300: return None
    subjects = o.get("subjects")
    subjects = [s.strip()[:40] for s in subjects if isinstance(s, str) and s.strip()][:3] if isinstance(subjects, list) else []
    try:
        duration = max(0.0, min(120.0, float(o.get("duration_s") or 0)))
    except (TypeError, ValueError):
        return None
    title = o.get("title") if isinstance(o.get("title"), str) and o["title"].strip() else sentence
    return {"title": title.strip()[:60], "sentence": sentence, "subjects": subjects, "question": q.strip(), "duration_s": duration,
            "conditions": valid_conditions(o.get("conditions"), subjects),
            "sound_labels": [x for x in o.get("sound_labels") or [] if x in meta.SOUND_LABELS][:5] if isinstance(o.get("sound_labels"), list) else [],
            "uses_sound": o.get("uses_sound") is True, "uses_motion": o.get("uses_motion") is True}


def parse_heard(raw):
    """What the browser's recognizer reports. It comes from the visitor, so keep only clean, known-shaped values."""
    try:
        o = json.loads(raw)
        scores = {k[:60]: float(v) for k, v in list(o["scores"].items())[:12] if isinstance(k, str) and 0 <= float(v) <= 1}
        return {"ms": max(0, min(10000, int(o.get("ms", 0)))), "scores": scores}
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def heard_match(spec, heard):
    """Plain code: did the recognizer hear a sound this rule wants? Returns (label, score) or None."""
    scores, hits = heard["scores"], []
    for x in spec.get("sound_labels", []):
        if scores.get(x, 0) >= HEARD_MIN: hits.append((x, scores[x]))
        hits += [(x, scores[other]) for other, bar in SOUND_ALSO.get(x, {}).items() if scores.get(other, 0) >= bar]
    return max(hits, key=lambda h: h[1]) if hits else None


def valid_conditions(raw, subjects):
    out = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict) or c.get("subject") not in subjects: continue
        lo, hi = c.get("min"), c.get("max")
        ok = lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 16
        if ok(lo) or ok(hi):
            out.append({"subject": c["subject"], "min": lo if ok(lo) else None, "max": hi if ok(hi) else None})
    return out[:3]


def unmet_condition(spec, counts, seen_at, now):
    """Plain code, not a model: SAM's fresh count can overrule Spark's yes. Returns the reason, or None."""
    for c in spec.get("conditions", []):
        n = counts.get(c["subject"])
        if n is None or now - seen_at.get(c["subject"], 0) > FRESH_S: continue
        if (c["min"] is not None and n < c["min"]) or (c["max"] is not None and n > c["max"]):
            return f"SAM counts {n} {c['subject']}, so this isn't happening."
    return None


def friendly(e):
    """A plain-language cause, never the raw error (which could echo request details)."""
    status = getattr(e, "status_code", None)
    if status in (401, 403): return "Meta's API rejected the key. Check the API key setting on the server."
    if status == 402: return "Meta's API says the account is out of credit. Check the Billing page."
    if status == 404: return "Meta's models didn't answer this check. Trying again."
    if status == 429: return "Meta's API is rate limiting us. Wait a moment and try again."
    if type(e).__name__ in ("APITimeoutError", "APIConnectionError"): return "Couldn't reach Meta's API. Check the connection and try again."
    return f"Meta's API had a problem with that request (error {status or type(e).__name__}). Try again in a moment."


def sample_reply(reason):
    return {"sample": True, "reason": reason}


class Start(BaseModel):
    rules: list[str]
    consent: bool = False


def compile_valid(sentence, led):
    """A model reply can come back empty or malformed, so it gets one more try before we bother the person."""
    for _ in range(2):
        spec = validate_spec(meta.compile_rule(sentence, led), sentence)
        if spec: return spec
    return None


def drop_stale(now):
    with sessions_lock:
        for sid in [k for k, s in sessions.items() if now - s["seen"] > 60 or now - s["t0"] > MAX_SESSION_MIN * 60 + 60]:
            del sessions[sid]  # frames go with the session; they're never written to disk
        return sum(1 for s in sessions.values() if now - s["seen"] < ACTIVE_S)


@app.post("/api/start")
def start(body: Start, request: Request):
    ip, now = client_ip(request), time.time()
    if not limiter.allow(f"start:{ip}", 10, 60): raise HTTPException(429, "Too many starts. Wait a minute and try again.")
    if not body.consent: raise HTTPException(400, "Please confirm you're only filming people who agreed.")
    rules = [r.strip() for r in body.rules if isinstance(r, str) and r.strip()]
    if not rules: raise HTTPException(400, "Add at least one rule.")
    if len(rules) > MAX_RULES: raise HTTPException(400, f"Use at most {MAX_RULES} rules.")
    if any(len(r) > MAX_RULE_CHARS for r in rules): raise HTTPException(400, f"Keep each rule under {MAX_RULE_CHARS} characters.")
    if SAMPLE_ONLY: return sample_reply("Live watching is switched off right now.")
    if reason := budget.blocked(ip): return sample_reply(reason)
    if drop_stale(now) >= MAX_CONCURRENT: return sample_reply("Vivre is busy with other live sessions right now.")

    led = meta.Ledger(on_cost=lambda c: budget.charge(ip, c))
    t0 = time.time()
    try:
        raw = list(compile_pool.map(lambda r: compile_valid(r, led), rules))
    except Exception as e:
        print(f"Rule compile failed: {meta.describe(e)}")
        if getattr(e, "status_code", None) == 404:  # Meta isn't offering models to this server right now
            return sample_reply("Meta's models aren't answering right now, which sometimes lasts a few minutes.")
        raise HTTPException(502, friendly(e))
    specs = raw
    for s, r in zip(specs, rules):
        if s is None: raise HTTPException(422, f'Couldn\'t turn this into a rule, try rewording it: "{r[:80]}"')
    ms = meta.now_ms(t0)
    sid = uuid.uuid4().hex
    S = {"specs": specs, "timers": [RuleTimer(s["duration_s"]) for s in specs], "expl": [""] * len(specs),
         "t0": now, "seen": now, "last": None, "last_call": 0, "last_tick": 0, "n": 0, "led": led, "ip": ip,
         "frames": deque(maxlen=3), "counts": {}, "seen_at": {}, "boxes": {}, "audio": None, "heard": None,
         "sam_pending": 0, "sam_ok": None, "sam_ms": None,
         "lock": threading.Lock(), "busy": threading.Lock(), "out": None, "log": [], "watch": uuid.uuid4().hex,
         "cards": [{"status": "ok", "held": 0, "need": s["duration_s"], "note": ""} for s in specs],
         "stages": {"understand": {"model": meta.COMPILER, "state": "ok", "ms": ms}}}
    with sessions_lock: sessions[sid] = S
    return {"session": sid, "watch": S["watch"], "cards": [{"title": s["title"], "sentence": r, "need": s["duration_s"], "question": s["question"],
                                       "subjects": s["subjects"], "conditions": s["conditions"], "uses_sound": s["uses_sound"],
                                       "sound_labels": s["sound_labels"]}
                                      for s, r in zip(specs, rules)],
            "ledger": led.report(), "total": round(led.total, 4), "stages": S["stages"]}


def sam_one(S, phrase, jpg):
    t0 = time.time()
    boxes = meta.segment(jpg, phrase, S["led"])
    with S["lock"]:
        S["sam_pending"] -= 1
        S["sam_ms"] = meta.now_ms(t0)
        S["sam_ok"] = boxes is not None
        if boxes is not None:
            S["counts"][phrase], S["boxes"][phrase], S["seen_at"][phrase] = len(boxes), boxes, time.time()


def by_ear(S, spec):
    return bool(spec.get("sound_labels")) and S.get("heard") is not None


def start_sam(S, jpg):
    """SAM runs beside the check; its results feed the next one, so it never slows a check down."""
    looked_at = [s for s in S["specs"] if not by_ear(S, s)]  # a rule decided by ear needs no object counts
    wanted = [c["subject"] for s in looked_at for c in s.get("conditions", [])] + [x for s in looked_at for x in s["subjects"]]
    subjects = list(dict.fromkeys(wanted))[:3]  # the ones that decide something come first
    with S["lock"]:
        if S["sam_pending"] or not subjects: return
        S["sam_pending"] = len(subjects)
    for x in subjects: sam_pool.submit(sam_one, S, x, jpg)


def ear_answer(spec, heard):  # plain code decides; no model is asked
    hit = heard_match(spec, heard)
    if hit: return {"yes": True, "note": f"Heard {hit[0].lower()} ({round(hit[1] * 100)}% sure)."}
    return {"yes": False, "note": f"Listening for {spec['sound_labels'][0].lower()}. Not hearing it."}


def explain_later(S, i, spec, alert):
    """Wonder writes the explanation in the background, so a slow answer never holds up the alert."""
    S["stages"]["confirm"] = {"model": meta.JUDGE, "state": "waiting", "ms": None}
    frames, audio, heard, t0 = list(S["frames"]), S["audio"], alert["say"], time.time()

    def work():
        try:
            v = meta.confirm(spec["question"], frames, heard, S["led"], audio=audio, recognized=heard)
            S["expl"][i] = alert["say"] = v["explanation"]
            S["stages"]["confirm"] = {"model": meta.JUDGE, "state": "ok", "ms": meta.now_ms(t0)}
        except Exception as e:
            print(f"Wonder explanation failed: {meta.describe(e)}")
            S["stages"]["confirm"] = {"model": meta.JUDGE, "state": "unavailable", "ms": meta.now_ms(t0)}
    compile_pool.submit(work)


def run_check(S, jpg, diff, now):
    """One check for one session. No HTTP here, so tests can run it with stubbed models."""
    t, led, specs = round(now - S["t0"], 1), S["led"], S["specs"]
    S["frames"].append(jpg)
    listen = S["audio"] if any(s.get("uses_sound") for s in specs) else None
    # a still picture says nothing about a sound, so sound rules always get a real look
    reused = bool(S["last"]) and not listen and diff < STILL_DIFF and now - S["last_call"] < STILL_MAX_S
    stages, problem = S["stages"], None
    frames = list(S["frames"])[-2:] if any(s.get("uses_motion") for s in specs) else [jpg]
    if reused:
        answers = S["last"]; led.skipped += 1
    else:
        S["n"] += 1
        start_sam(S, jpg)
        with S["lock"]: counts = ", ".join(f"{k} x{v}" for k, v in S["counts"].items())
        t0 = time.time()
        try:
            asked = [s for s in specs if not by_ear(S, s)]
            answers = meta.check_frame([s["question"] for s in asked], frames, counts, led, audio=listen) if asked else []
            if asked:
                stages["judge"] = {"model": meta.COMPILER, "state": "ok" if answers else "failed", "ms": meta.now_ms(t0),
                                   "sound": bool(listen)}
        except Exception as e:
            print(f"Spark check failed: {meta.describe(e)}")
            answers, problem = None, friendly(e)
            stages["judge"] = {"model": meta.COMPILER, "state": "failed", "ms": meta.now_ms(t0)}
        if answers:
            S["last"], S["last_call"] = answers, now
        elif answers is None and not problem:
            problem = "Spark's answer wasn't usable on this check. Trying again."
    with S["lock"]:
        see = "unavailable" if S["sam_ok"] is False else "ok" if S["sam_ok"] else "waiting"
        fresh = {k: S["counts"][k] for k in S["counts"] if now - S["seen_at"].get(k, 0) <= FRESH_S}
        boxes = [{"label": k, "box": b} for k in fresh for b in S["boxes"][k]]
        if not any(not by_ear(S, s) for s in specs): see = "idle"  # nothing to look at when sound decides every rule
        stages["see"] = {"model": meta.SEG, "state": see, "ms": S["sam_ms"], "counts": fresh, "overruled": False}

    if S.get("heard"):
        top = max(((k, v) for k, v in S["heard"]["scores"].items() if k != "Silence"), key=lambda kv: kv[1], default=None)
        stages["listen"] = {"model": "YAMNet, in your browser", "state": "ok", "ms": S["heard"]["ms"],
                            "top": f"{top[0]} {round(top[1] * 100)}%" if top else "Quiet"}
    if answers is not None:  # rules decided by ear don't have a model answer, so plain code fills theirs in
        pending = iter(answers)
        answers = [ear_answer(s, S["heard"]) if by_ear(S, s) else next(pending, {"yes": False, "note": ""}) for s in specs]

    new = []
    if answers:  # with no usable answer the timers wait; a failed check isn't evidence either way
        cards = []
        for i, (s, a) in enumerate(zip(specs, answers)):
            timer, note, yes = S["timers"][i], a["note"], a["yes"]
            if yes and (why := unmet_condition(s, S["counts"], S["seen_at"], now)):
                yes, note, stages["see"]["overruled"] = False, why, True
            state, held = timer.step(yes, t)
            if state == "due" and by_ear(S, s):  # the recognizer decided, so alert now; Wonder explains afterward
                timer.confirmed(t, SOUND_ALERT_HOLD_S)
                S["expl"][i], state = note, "alert"
                alert = {"id": len(S.setdefault("log", [])) + 1, "i": i, "title": s["title"], "say": note, "t": t}
                S["log"].append(alert); new.append(alert)
                explain_later(S, i, s, alert)
            elif state == "due":
                t0 = time.time()
                try:
                    v = meta.confirm(s["question"], list(S["frames"]), note, led, audio=S["audio"] if s.get("uses_sound") else None)
                    stages["confirm"] = {"model": meta.JUDGE, "state": "ok", "ms": meta.now_ms(t0)}
                except Exception as e:  # say so on screen and alert on Spark's word alone
                    print(f"Wonder confirm failed: {meta.describe(e)}")
                    v = {"confirm": True, "explanation": note}
                    stages["confirm"] = {"model": meta.JUDGE, "state": "unavailable", "ms": meta.now_ms(t0)}
                if v["confirm"]:
                    timer.confirmed()
                    S["expl"][i], state = v["explanation"], "alert"
                    alert = {"id": len(S.setdefault("log", [])) + 1, "i": i, "title": s["title"], "say": v["explanation"], "t": t}
                    S["log"].append(alert); new.append(alert)
                else:
                    timer.rejected(t)
                    state, held, note = "ok", 0.0, f"Double-checked, not confirmed. {v['explanation']}"
            if state == "alert": note = S["expl"][i] or note
            cards.append({"status": "watch" if state == "due" else state, "held": held, "need": timer.need, "note": note})
        S["cards"] = cards
    return {"t": t, "cards": S["cards"], "new_alerts": new, "reused": reused, "skipped": led.skipped, "problem": problem,
            "stages": stages, "boxes": boxes, "log": S.get("log", []), "ledger": led.report(), "total": round(led.total, 4)}


@app.post("/api/tick")
def tick(request: Request, session: str = Form(...), diff: float = Form(1.0), image: UploadFile = File(...),
         audio: UploadFile | None = File(None), heard: str | None = Form(None)):
    ip, now = client_ip(request), time.time()
    if not limiter.allow(f"tick:{ip}", 60, 60): raise HTTPException(429, "Too many checks. Wait a moment.")
    S = sessions.get(session)
    if not S: raise HTTPException(404, "This session has ended. Press Start watching again.")
    jpg = image.file.read(MAX_FRAME_BYTES + 1)
    if len(jpg) > MAX_FRAME_BYTES: raise HTTPException(413, "That frame is too large.")
    if jpg[:3] != b"\xff\xd8\xff": raise HTTPException(400, "That frame isn't a JPEG image.")
    clip = read_wav(audio, MAX_CLIP_BYTES) if audio else None
    if not S["busy"].acquire(blocking=False):  # the previous check is still running
        if S["out"]: return S["out"]
        raise HTTPException(429, "Still working on the first check.")
    try:
        S["seen"] = now
        if now - S["t0"] > MAX_SESSION_MIN * 60:
            with sessions_lock: sessions.pop(session, None)
            return sample_reply(f"Live sessions stop after {MAX_SESSION_MIN:g} minutes to keep the demo affordable.")
        if SAMPLE_ONLY: return sample_reply("Live watching is switched off right now.")
        if reason := budget.blocked(ip, S["led"].total):
            with sessions_lock: sessions.pop(session, None)
            return sample_reply(reason)
        if S["out"] and now - S["last_tick"] < MIN_CHECK_S: return S["out"]
        S["last_tick"] = now
        S["heard"] = parse_heard(heard) if heard else None
        S["audio"] = clip  # only the latest few seconds, and only in memory
        S["out"] = run_check(S, jpg, diff, now)
        return S["out"]
    finally:
        S["busy"].release()


def watch_view(S):
    """What a second device may see: card states and alerts. Never frames, sound or costs."""
    return {"cards": [{"title": s["title"], "sentence": s.get("sentence", ""), "question": s["question"]} for s in S["specs"]],
            "states": S["cards"], "log": S.get("log", [])}


@app.get("/api/watch/{token}")
def watch(token: str, request: Request):
    if not limiter.allow(f"watch:{client_ip(request)}", 60, 60): raise HTTPException(429, "Too many refreshes. Wait a moment.")
    with sessions_lock:
        S = next((s for s in sessions.values() if s["watch"] == token), None)
    if not S: raise HTTPException(404, "This watch has ended.")
    return watch_view(S)


def read_wav(upload, limit):
    wav = upload.file.read(limit + 1)
    if len(wav) > limit: raise HTTPException(413, "That sound clip is too long.")
    if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE": raise HTTPException(400, "That sound clip isn't a WAV file.")
    return wav


@app.post("/api/transcribe")
def spoken_rule(request: Request, audio: UploadFile = File(...)):
    ip = client_ip(request)
    if not limiter.allow(f"voice:{ip}", 6, 60): raise HTTPException(429, "Too many recordings. Wait a minute, or type the rule.")
    if SAMPLE_ONLY or (reason := budget.blocked(ip)): raise HTTPException(503, "Voice isn't available right now. Please type the rule.")
    wav = read_wav(audio, MAX_SPOKEN_BYTES)
    led, t0 = meta.Ledger(on_cost=lambda c: budget.charge(ip, c)), time.time()
    try:
        text = meta.transcribe(wav, led)
    except Exception as e:
        print(f"Voice Transcribe failed: {meta.describe(e)}")
        raise HTTPException(502, "Voice Transcribe didn't answer. Please type the rule.")
    if not text: raise HTTPException(422, "I didn't catch that. Try again, or type the rule.")
    return {"text": text[:MAX_RULE_CHARS], "stage": {"model": meta.VOICE, "state": "ok", "ms": meta.now_ms(t0)},
            "ledger": led.report(), "total": round(led.total, 6)}


_visible = {"at": 0.0, "n": None}

@app.get("/api/probe-temp")
def probe_temp():
    import diag_probe  # temporary, removed right after this test
    if not limiter.allow("probe", 4, 60): raise HTTPException(429, "Wait a moment.")
    return diag_probe.run()


@app.get("/healthz")
def healthz():
    """Free: how many models Meta offers this server right now, checked at most every 20 seconds."""
    if time.time() - _visible["at"] > 20:
        _visible["at"] = time.time()
        try:
            _visible["n"] = len(meta.client.models.list().data)
        except Exception as e:
            _visible["n"] = None
            print(f"Couldn't list models: {meta.describe(e)}")
    return {"ok": True, "models": _visible["n"]}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
