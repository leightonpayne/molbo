---
icon: lucide/funnel
---

# Sharing Sessions

`molbo` runs a normal HTTP server, so sharing a session is mostly about how you expose that server to another device or user.

<div class="admonition warning">
  <p class="admonition-title">Warning</p>
  <p>If you expose a session publicly, anyone with the link can access it while it is running. <code>molbo</code> does not implement authentication.</p>
</div>

## Share on your tailnet

Bind to a Tailscale-reachable address and choose a fixed port:

```bash
molbo 1crn --host 100.x.y.z --port 8080 --no-open --base-url http://100.x.y.z:8080
```

Anyone on the same tailnet can then open:

```
http://100.x.y.z:8080
```

## Share publicly with Tailscale Funnel

Run `molbo` locally:

```bash
molbo 1crn --host 127.0.0.1 --port 8080 --no-open
```

Then expose that port with Funnel:

```bash
tailscale funnel 8080
```

Tailscale will give you a public `https://...ts.net` URL that proxies to the local session.

## Share through another reverse proxy or tunnel

You can also put `molbo` behind:

- Cloudflare Tunnel
- Caddy
- nginx
- any reverse proxy that forwards HTTP traffic to the local server

## Share multiple sessions on one domain with path prefixes

You can also mount separate `molbo` processes under different paths on the same hostname:

```text
https://molbo.example.com/s/demo-1/
https://molbo.example.com/s/demo-2/
```

Start each session with a distinct path in `--base-url`:

```bash
molbo 1crn --host 127.0.0.1 --port 8081 --no-open \
  --base-url https://molbo.example.com/s/demo-1

molbo 4hhb --host 127.0.0.1 --port 8082 --no-open \
  --base-url https://molbo.example.com/s/demo-2
```

Then configure your reverse proxy so each path prefix forwards to the matching local port.

This is still one `molbo` process per session. `molbo` does not yet include a built-in multi-session registry or router.
