// youtubelink.gabemills.com → GM Study Hall on the Massed Compute VM, over the Cloudflare tunnel (VPC service).
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const target = `http://127.0.0.1:8080${url.pathname}${url.search}`;
    try {
      return await env.APP.fetch(new Request(target, request));
    } catch (err) {
      return new Response(
        "GM Study Hall is offline right now (the VM or its tunnel is down). Try again in a minute.",
        { status: 502, headers: { "content-type": "text/plain; charset=utf-8" } },
      );
    }
  },
};
