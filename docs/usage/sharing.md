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

Typical local target:

```
http://127.0.0.1:8080
```

If the public URL differs from the local bind address, pass it to `molbo` explicitly:

```bash
molbo 1crn --host 127.0.0.1 --port 8080 --base-url https://mol.leightonpayne.com
```

## Notes

- Without `--base-url`, the terminal banner and QR button use the local viewer URL.
- With `--base-url`, the terminal banner shows both the local and share URLs, and the QR button encodes the share URL even if you opened the session locally first.
- `molbo` does not currently implement authentication. If you expose a session publicly, anyone with the link can access it while it is running.
