"""Temporary: compare the normal route to Meta's API with a route to a different Meta server. Outcomes only, never the key."""
import os, socket, time
import httpx2

ALT_IPS = ["157.240.22.19", "31.13.70.8"]
BODY = {"model": "muse-spark-1.2", "input": "Say ok", "max_output_tokens": 200}


def run():
    key = os.getenv("MODEL_API_KEY", "").strip()
    head = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    def attempt(fn):
        try:
            r = fn()
            return [r.status_code, r.headers.get("x-fb-debug", "")[:4], r.text[:60] if r.status_code != 200 else ""]
        except Exception as e:
            return type(e).__name__
    out = {"dns": sorted({a[4][0] for a in socket.getaddrinfo("api.meta.ai", 443)})}
    with httpx2.Client(timeout=15) as c:
        out["normal"] = [attempt(lambda: c.post("https://api.meta.ai/v1/responses", json=BODY, headers=head)) for _ in range(3)]
        for ip in ALT_IPS:
            out[ip] = [attempt(lambda: c.post(f"https://{ip}/v1/responses", json=BODY, headers={**head, "Host": "api.meta.ai"},
                                              extensions={"sni_hostname": "api.meta.ai"})) for _ in range(3)]
    return out
