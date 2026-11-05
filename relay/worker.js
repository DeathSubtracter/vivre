export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/v1/")) return new Response("Not found", { status: 404 });
    const headers = new Headers(request.headers);
    for (const name of [...headers.keys()]) {
      if (name === "host" || name.startsWith("cf-") || name.startsWith("x-forwarded") || name === "x-real-ip") headers.delete(name);
    }
    return fetch("https://api.meta.ai" + url.pathname + url.search, {
      method: request.method,
      headers,
      body: request.body,
    });
  },
};
