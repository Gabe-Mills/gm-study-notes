// GM Study Hall: the public domain → the app on the VM, over the Cloudflare tunnel (VPC service).
// When MAIN_HOST is set, every other hostname (the old youtubelink.gabemills.com, www) 301s to it.
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (env.MAIN_HOST && url.hostname !== env.MAIN_HOST) {
      return Response.redirect(`https://${env.MAIN_HOST}${url.pathname}${url.search}`, 301);
    }
    const target = `http://127.0.0.1:8080${url.pathname}${url.search}`;
    try {
      return await env.APP.fetch(new Request(target, request));
    } catch (err) {
      return new Response(
        "GM Study Hall is offline right now. Try again in a minute.",
        { status: 502, headers: { "content-type": "text/plain; charset=utf-8" } },
      );
    }
  },
};
