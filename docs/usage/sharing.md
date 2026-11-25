---
icon: lucide/funnel
---

# Sharing Sessions

`molbo` runs a normal HTTP server, so sharing a session is mostly about how you expose that server to another device or user.

<div class="admonition warning">
  <p class="admonition-title">Warning</p>
  <p>If you expose a session publicly, anyone with the link can access it while it is running. <code>molbo</code> does not implement authentication.</p>
</div>

## Share on your local network

Bind to `0.0.0.0` to accept connections from other devices on the same Wi-Fi or lab network. `molbo` automatically detects your LAN IP for the banner and QR code, and `--qr` prints a scannable QR code right in your terminal:

```bash
molbo 1crn --host 0.0.0.0 --qr
```

## Share on your tailnet

Bind to a Tailscale-reachable address and choose a fixed port:

```bash
molbo 1crn --host 100.x.y.z --port 8080 --no-open --qr
```

Anyone on the same tailnet can then open:

```text
http://100.x.y.z:8080
```

Add `--base-url` when viewers should use a different public hostname, HTTPS URL, or path prefix (for example behind a reverse proxy or Tailscale Funnel):

```bash
molbo 1crn --host 127.0.0.1 --port 8080 --no-open --base-url https://molbo.example.com
```

## Private unguessable links

When exposing an unpublished structure over a public tunnel or Funnel, pass `--private` to mount the session under a random path token (while returning `404` at `/`):

```bash
molbo secret.cif --port 8080 --no-open --private --base-url https://my-machine.ts.net --qr
```

## Share a specific camera view or download the file

- Open the QR / share dialog in the viewer topbar and check **Include current camera view** to encode the current Mol* camera angle in the URL hash (`#cam=...`).
- Recipients can click the **Download** button in the topbar to save the structure file locally.

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

Because the viewer uses relative asset and API paths (`./structure`, `./assets/...`), both prefix-preserving reverse proxies (with `--base-url`) and prefix-stripping proxies (such as `tailscale funnel --set-path=/demo 8080`, Caddy `handle_path`, or nginx `proxy_pass http://127.0.0.1:8081/;`) work cleanly. When passing `--base-url` with a path prefix, forward without stripping the prefix:

```caddyfile
molbo.example.com {
    handle /s/demo-1* {
        reverse_proxy 127.0.0.1:8081
    }
    handle /s/demo-2* {
        reverse_proxy 127.0.0.1:8082
    }
}
```

Or with nginx (note the lack of a trailing `/` on `proxy_pass`, which preserves the prefix):

```nginx
location /s/demo-1 {
    proxy_pass http://127.0.0.1:8081;
}
location /s/demo-2 {
    proxy_pass http://127.0.0.1:8082;
}
```

This is still one `molbo` process per session. `molbo` does not yet include a built-in multi-session registry or router.
