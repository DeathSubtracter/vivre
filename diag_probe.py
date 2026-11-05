"""Temporary: compare the normal route to Meta's API with a route to a different Meta server. Outcomes only, never the key."""
import os, time
import httpx2
import meta

ALT_IP = "157.240.22.19"
BODY = {"model": "muse-spark-1.2", "input": "Say ok", "max_output_tokens": 200}


def run():
    key = os.getenv("MODEL_API_KEY", "").strip()
    head = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    def attempt(fn):
        try:
            return fn().status_code
        except Exception as e:
            return type(e).__name__
    with httpx2.Client(timeout=20) as c:
        normal = attempt(lambda: c.post("https://api.meta.ai/v1/responses", json=BODY, headers=head))
        other = attempt(lambda: c.post(f"https://{ALT_IP}/v1/responses", json=BODY, headers={**head, "Host": "api.meta.ai"},
                                       extensions={"sni_hostname": "api.meta.ai"}))
    return {"normal": normal, "other_server": other, "at": time.strftime("%H:%M:%S")}
