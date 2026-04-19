# PocketCodex

PocketCodex is a personal remote-control prototype for using the Codex app on a Mac from a phone browser.

The current milestone focuses on the smallest end-to-end loop that is actually useful:

- live stream the Mac screen or Codex window to a phone browser
- use a stream-first phone console with native control buttons
- have a local Mac agent focus the Codex app and paste the prompt
- optionally submit the prompt automatically

## Status

Implemented and smoke-tested locally:

- FastAPI service with session management
- token-protected host, viewer, and agent access per session
- session ids are write-once so a guessed id cannot be reclaimed for a token
- WebSocket signaling for WebRTC offer/answer exchange
- launch page that creates a session id and displays signed host/viewer links
- QR-ready viewer links generated from the active session
- browser host page for Mac screen sharing
- mobile viewer page for watching the stream, taking control, and sending native actions
- single-controller lease so only one connected viewer can issue commands at a time
- presence and recent-command status in the phone UI
- Mac agent that polls for commands and can inject prompts, focus Codex, and send Escape via AppleScript
- temporary Cloudflare Quick Tunnel-style remote trial flow
- API smoke test for session, command queue, and WebSocket relay flows

Not done yet:

- native macOS app packaging
- TURN/STUN hardening for internet-grade connectivity beyond trial tunnels
- full remote mouse and keyboard control
- persistent auth and multi-device account management

## Architecture

The prototype is split into three pieces:

1. FastAPI control plane
   - serves the web UI
   - stores session metadata and pending commands
   - issues an unguessable access token per session
   - refuses duplicate session ids so links cannot be re-minted by id alone
   - tracks host, viewer, and agent heartbeats
   - tracks which viewer currently holds the control lease
   - relays WebRTC signaling messages over WebSocket
   - can start a remote trial tunnel and swap viewer links to that public URL

2. Mac host
   - opens the host page on `localhost` in a browser
   - captures the selected screen or app window with `getDisplayMedia`
   - exposes separate actions for window sharing and full-screen sharing
   - displays a QR code for the token-protected phone viewer link
   - keeps using `localhost` for the host page even when a remote trial tunnel is active
   - streams the media to the phone browser using WebRTC

3. Mac agent
   - runs locally on the Mac
   - polls the FastAPI service for queued commands
   - authenticates using the session token
   - activates the `Codex` app
   - supports native `focus` and `stop` style control actions from the phone viewer
   - replaces the existing draft in the focused Codex prompt field before pasting
   - optionally presses Return to submit

## Repo Layout

- `shared-backend/app/main.py`: FastAPI app, session store, command queue, signaling server
- `shared-backend/mac_agent.py`: Mac-side Codex prompt injector
- `shared-backend/web/host.html`: host page used on the Mac
- `shared-backend/web/viewer.html`: mobile viewer/control page
- `shared-backend/web/index.html`: landing page
- `shared-backend/scripts/smoke_test.py`: local API smoke test
- `shared-backend/scripts/fake_tunnel.py`: deterministic test helper for the remote trial flow

## Quick Start

### 1. Install and run the server

```bash
cd shared-backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Optional environment variables for off-network access and more reliable streaming:

```bash
export PUBLIC_BASE_URL="https://your-public-host.example.com"
export ICE_SERVERS_JSON='[{"urls":"stun:stun.l.google.com:19302"},{"urls":["turn:turn.example.com:3478?transport=udp","turns:turn.example.com:5349?transport=tcp"],"username":"user","credential":"pass"}]'
```

Recommended secure deployment shape:

- put PocketCodex behind HTTPS at `PUBLIC_BASE_URL`
- keep the generated tokenized links private
- configure your own TURN server in `ICE_SERVERS_JSON`
- prefer a dedicated small VPS or Cloudflare/Tailscale-style entry point over exposing a raw home IP

For quick public testing without running your own infra, PocketCodex now has a temporary remote trial mode:

- it starts a public tunnel to the local FastAPI service
- it keeps the Mac host page on `localhost`
- it switches the adaptive phone viewer link and QR code to the public tunnel URL
- it still shows a separate same-Wi-Fi viewer link and QR code

The current implementation prefers `cloudflared` if present and otherwise falls back to `npx wrangler@latest tunnel quick-start`.

### 2. Start the Mac agent

```bash
cd shared-backend
source .venv/bin/activate
python3 mac_agent.py --session demo123 --token YOUR_SESSION_TOKEN
```

For a dry run that does not type into the Codex app:

```bash
python3 mac_agent.py --session demo123 --token YOUR_SESSION_TOKEN --dry-run
```

### 3. Open the host page on the Mac

Open:

- `http://127.0.0.1:8000/`

Create a session there, then use the generated host and viewer links.

The launch page now also shows:

- the localhost host link meant for the Mac
- the public/LAN viewer link meant for the phone
- the viewer QR code itself for immediate phone scanning
- a second viewer QR code for same-Wi-Fi testing
- the viewer QR code URL
- the host public fallback URL
- the exact `mac_agent.py` command including the session token and localhost base URL
- controls to start and stop a remote trial tunnel

### 4. Open the host page on the Mac

Use the generated localhost host link on the Mac and choose the Codex window or the full screen
when the browser asks what to share.

The host page now offers:

- `Share Window` when you want just the Codex app window
- `Share Entire Screen` when you want the whole desktop

Safari still controls the final picker, but the UI now makes the intended choice explicit.

### 5. Open the viewer page on the phone

Use the generated viewer link from the launch page.

If you are testing outside your local network, put the server behind a secure tunnel or relay.

The viewer is now a hybrid remote console:

- the live stream stays front and center
- only one viewer at a time can `Take Control`
- native buttons on the phone can `Send`, `Paste Draft`, `Focus`, and `Stop`
- additional viewers can still watch, but they stay read-only until they take control

### 6. Remote Trial

From the launch page you can click `Start Remote Trial`.

That does three things:

- starts a temporary public tunnel to the local PocketCodex server
- updates the adaptive viewer link and QR code to the public tunnel URL
- keeps the separate same-Wi-Fi viewer link available as a fallback

Important limitations:

- the trial lasts only while the tunnel process is alive
- the public tunnel URL is internet-reachable, so the session token in the generated link is still sensitive
- this is meant for testing and demos, not production-grade uptime

## Local Testing

Run the smoke test:

```bash
cd shared-backend
source .venv/bin/activate
python3 scripts/smoke_test.py
```

This validates:

- health endpoint
- session creation
- duplicate session rejection
- heartbeat updates
- command enqueue
- command claim
- command completion
- single-controller lease behavior for viewer-issued commands
- token-protected access control
- WebSocket viewer/host message relay
- QR SVG generation
- remote trial start and stop behavior using a fake tunnel helper
- static pages

## macOS Permissions

The streaming host page needs browser permission to share the screen or window.

The Mac agent needs macOS Accessibility permission to send keystrokes to the Codex app.

At the current prototype stage, the browser-level flow works until Safari reaches the native
screen/window selection step. That chooser is expected to require human interaction during real
stream testing.

## Current Feature Notes

- The phone viewer is now stream-first, with a native bottom control surface instead of only a raw prompt form.
- Only the active controller can queue commands; other connected viewers remain read-only until they take control.
- The phone viewer currently sends structured Codex actions rather than arbitrary raw mouse or keyboard events.
- The stream is browser-based, so it does not yet require a packaged macOS app.
- The Mac agent targets an app named `Codex` by default.
- Safari screen sharing works when the host page is opened on `localhost` or HTTPS. The app now generates a localhost host link for the Mac by default and warns if the host page is opened from a plain LAN HTTP origin.
- The host page now has separate buttons for window sharing and full-screen sharing to reduce ambiguity in Safari's picker flow.
- Prompt injection now replaces the existing Codex draft by default, which avoids accidental prompt concatenation during phone control.
- The viewer page now shows recent command results, controller status, and whether the host and agent appear online.
- Set `PUBLIC_BASE_URL` to a public HTTPS URL if you want QR codes that open correctly off-network.
- The launch page now offers both an adaptive viewer QR and a same-Wi-Fi viewer QR. When a remote trial tunnel is active, the adaptive QR points at the public tunnel.
- For reliable cross-network streaming, configure a TURN server in `ICE_SERVERS_JSON`.
- Reusing a session now means reopening its original signed host/viewer link, not recreating the session by id.

## Near-Term Roadmap

- move from polling to a persistent control socket for the Mac agent
- add basic remote click targets for common Codex actions
- add a small Swift menu-bar app so the Mac side is one install instead of a browser page plus script
- bundle a hosted relay path so the public URL and TURN config work without manual env setup
