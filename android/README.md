# Hermes Agent Android

Native Android client for Hermes Agent. The target is not just chat: this app is meant to port the useful parts of the Hermes web dashboard to mobile, especially model management and the embedded terminal, while reaching your custom Hermes service over a Tailnet.

## Current MVP

Implemented:

- Chat against the Hermes API Server adapter:
  - `GET /health`
  - `POST /api/sessions`
  - `POST /api/sessions/{session_id}/chat`
- Dashboard model management:
  - `GET /api/status`
  - `GET /api/model/info`
  - `GET /api/model/options`
  - `POST /api/model/set`
- Dashboard terminal bridge:
  - WebSocket `/api/pty?token=...`
  - Sends dashboard resize escape `ESC[RESIZE:100;32]`
  - Basic ANSI stripping for mobile display
- Tailnet-first configuration:
  - Separate API Server URL and Dashboard URL
  - `tailscale://` shortcut to open the Android Tailscale client
  - Works over Tailnet by using normal HTTP/WebSocket through the Android VPN route

## Tailnet notes

I checked Maven Central for an official third-party `com.tailscale` Android SDK artifact and did not find one. The practical Android integration today is:

1. Install/login to the official Tailscale Android app.
2. Keep the Tailscale VPN active.
3. Configure this app with your node's Tailnet name or `100.x.y.z` IP.

Example URLs:

- API Server: `http://n01-gra-fr:8642` or `http://100.x.y.z:8642`
- Dashboard: `http://n01-gra-fr:9119` or `http://100.x.y.z:9119`

If you have a specific Tailnet SDK/library in mind, plug point is `DashboardClient` / `HermesApiClient` in `app/src/main/java/ai/hermes/android/MainActivity.kt`: replace the raw `HttpURLConnection` + OkHttp transport with that SDK's dialer.

## Hermes service setup

For the API Server chat surface, enable:

```bash
# ~/.hermes/.env
API_SERVER_ENABLED=true
API_SERVER_HOST=0.0.0.0
API_SERVER_PORT=8642
API_SERVER_KEY=change-me
```

For dashboard models + terminal, run the dashboard with TUI/PTY enabled and reachable on Tailnet. For a private Tailnet-only service, the rough shape is:

```bash
HERMES_DASHBOARD_TUI=1 hermes dashboard --host 0.0.0.0 --port 9119 --no-open --insecure --tui
```

Your custom service under `~/.hermes/` can run the equivalent command/systemd unit. The terminal tab needs the dashboard `/api/pty` endpoint; that endpoint is only active when dashboard TUI support is enabled.

## Dashboard authentication

The dashboard protects `/api/*` with the ephemeral `X-Hermes-Session-Token` that the web server injects into the browser bundle. This MVP accepts that token in settings and sends both:

- The `X-Hermes-Session-Token` header with the configured dashboard token.
- A bearer authorization header with the same dashboard token for backward compatibility.

For a polished mobile build, the next backend improvement should be a durable mobile token or OAuth/device-code flow for dashboard API clients. Right now, the app can already speak the dashboard protocol once you provide the session token used by your service.

## Build

Open the `android/` directory in Android Studio, or run with a local Android SDK + Gradle:

```bash
cd android
gradle :app:assembleDebug
```

## Important endpoints from the current dashboard

Verified in this repo:

- `hermes_cli/web_server.py`
  - `GET /api/model/info`
  - `GET /api/model/options`
  - `POST /api/model/set`
  - `WS /api/pty`
- `gateway/platforms/api_server.py`
  - `GET /health`
  - `POST /api/sessions`
  - `POST /api/sessions/{session_id}/chat`

## Next useful additions

- Proper mobile auth flow for dashboard APIs instead of pasted ephemeral token.
- Session/history UI from dashboard `/api/sessions`.
- Auxiliary model assignment UI from `/api/model/auxiliary` + `/api/model/set` with `scope=auxiliary`.
- Better terminal renderer using a VT/xterm parser instead of simple ANSI stripping.
- If a real Tailnet Android SDK is selected, replace the transport layer with SDK dialing rather than relying on the system VPN route.
