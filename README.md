# OpenBubblesProxy

A small self-hosted proxy that restores self-hosted OpenBubbles phone-number
registration using your own relay iPhone ([JJTech's ValidationRelay](https://github.com/JJTech0130/ValidationRelay)).

## How it works

Apple now rejects the identity the relay reports at the activation step. This
proxy sits between the OpenBubbles app and your registration relay and
rewrites the two fields Apple checks there (serial + UDID). It also returns
401 locally for `sign-data` so OpenBubbles uses its fallback instead of sending
an unsupported command to JJTech's ValidationRelay. Other requests pass through.
No devices are hardcoded: each relay iPhone gets its own identity, generated
and remembered automatically.

## Quick start (Docker + Cloudflare Tunnel)

1. Cloudflare Zero Trust -> Networks -> Tunnels -> create a tunnel, copy the
   token, and add a Public Hostname pointing at `HTTP -> ob-profile-proxy:8080`.
2. `cp .env.example .env` and set `TUNNEL_TOKEN`.
3. `docker compose up -d --build`
4. In OpenBubbles, set the relay URL to your `https://<hostname>` and enter
   the code shown on your iPhone's ValidationRelay app.

Bare Python (no Docker): `python proxy.py` — standard library only.

## Rules

- Never edit, delete, or copy `data/registry.json` entries between
  deployments. It maps each iPhone to the identity Apple activated exactly once.
- Re-registering the same phone? Delete its registry entry first so a
  fresh identity is issued. The old registration is then abandoned.
- Keep the proxy online — renewals fetch validation data through it.
- Keep the relay iPhone online with ValidationRelay connected (display on,
  signed out of Apple ID, iMessage/FaceTime off — per the
  [official docs](https://openbubbles.app/docs/pnr.html)).

## Notes

- `sign-data` returns 401 locally; the app falls back automatically. This
  avoids an unsupported-command timeout that can disconnect the relay iPhone.
- Apple can change its rules at any time; this is the same category of
  interoperability behavior as the client apps themselves.

## Setup instructions

### What you need

- A spare iPhone to act as the relay, running JJTech's ValidationRelay app
  (see the [official docs](https://openbubbles.app/docs/pnr.html)).
- A machine that stays on 24/7 to run the proxy (a homelab box, VPS, or Pi).
  Docker and Docker Compose are recommended; bare Python also works.
- A Cloudflare account with a domain, for the Tunnel (or Tailscale, see the
  alternative section at the end).
- The OpenBubbles app on the Android device you want to register.

### 1. Create the Cloudflare Tunnel

1. In Cloudflare, go to Zero Trust -> Networks -> Tunnels and create a new
   tunnel.
2. Copy the tunnel token. You will paste it into `.env` in the next step.
3. Add a Public Hostname (for example `ob.example.com`) with:
   - Service type: `HTTP`
   - URL: `ob-profile-proxy:8080`
4. Save. The hostname will start working once the tunnel container is up.

### 2. Configure and start the proxy

1. Clone or copy this project onto your server and open a shell in it.
2. Create your env file:

   ```bash
   cp .env.example .env
   ```

3. Edit `.env` and set `TUNNEL_TOKEN` to the token from step 1.
4. Build and start everything:

   ```bash
   docker compose up -d --build
   ```

**Without Docker:** run `python proxy.py` from the project root. It only uses
the standard library, so no installs are needed. You must then expose it over
HTTPS yourself (for example with your own tunnel or reverse proxy) and keep
the process running, using something like a systemd service.

### 3. Register in OpenBubbles

1. Open OpenBubbles and choose the self-hosted / relay registration option.
2. Set the relay URL to your public hostname, for example
   `https://ob.example.com`. Use `https`, with no trailing path.
3. Enter the code shown in the ValidationRelay app on your iPhone.
4. Follow the prompts to register and set up your phone number.
5. Watch `docker compose logs -f` while you do this. You should see requests
   passing through the proxy. A 401 on `sign-data` is expected and harmless.

On first contact the proxy generates a unique identity for your relay iPhone
and stores it in `data/registry.json`. Later sessions with the same iPhone
reuse that identity automatically.

### 4. Day-to-day operation

- Keep the proxy host, the Cloudflare Tunnel, and the relay iPhone online.
  Registration renewals fetch validation data through this chain, and if any
  part is down the renewal can fail.
- Keep the relay iPhone plugged in, on Wi-Fi, with the display on and
  ValidationRelay connected.

### Alternative: Tailscale instead of Cloudflare Tunnel

Skip step 1 and the Cloudflare container. Tailscale must already be running on
the proxy host.

1. In `docker-compose.yml`, add this under `ob-profile-proxy`:

   ```yaml
       ports:
         - "127.0.0.1:8080:8080"
   ```

2. Start only the proxy:

   ```bash
   docker compose up -d --build ob-profile-proxy
   ```

3. Expose it over HTTPS to your tailnet (the Android phone needs Tailscale on):

   ```bash
   tailscale serve --bg 8080
   ```

4. Use the printed `https://<machine>.<tailnet>.ts.net` URL as the relay URL in
   OpenBubbles (step 3).
