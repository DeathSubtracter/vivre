"""Temporary: how do calls to Meta's API behave from this machine? Shows outcomes only, never the key."""
import hashlib, os, socket, time
import meta


def run():
    c = meta.client.with_options(max_retries=0, timeout=20)
    def outcome(fn):
        t = time.time()
        try:
            fn(); r = "ok"
        except Exception as e:
            r = str(getattr(e, "status_code", type(e).__name__))
        return f"{r}:{time.time() - t:.1f}s"
    say = [{"role": "user", "content": "Say ok"}]
    key = os.getenv("MODEL_API_KEY", "").strip()
    return {"key": hashlib.sha256(key.encode()).hexdigest()[:6],
            "dns": sorted({a[4][0] for a in socket.getaddrinfo("api.meta.ai", 443)}),
            "lists": [len(c.models.list().data) for _ in range(4)],
            "spark12": [outcome(lambda: c.responses.create(model="muse-spark-1.2", input="Say ok", max_output_tokens=300)) for _ in range(5)],
            "spark13": [outcome(lambda: c.responses.create(model="muse-spark-1.3", input="Say ok", max_output_tokens=300)) for _ in range(3)],
            "chat": [outcome(lambda: c.chat.completions.create(model="muse-spark-1.2", messages=say, max_tokens=300)) for _ in range(3)]}
