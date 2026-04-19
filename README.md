# Pocket Mac

Pocket Mac is a personal remote-control prototype for using the Codex app on a Mac from a phone browser.

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
- launch page that owns the host flow directly
- QR-ready viewer links generated from the active session
- browser host flow for Mac screen sharing
- mobile viewer page for watching the stream, taking control, and sending native actions
- single-controller lease so only one connected viewer can issue commands at a time
- presence and recent-command status in the phone UI
- Mac agent that polls for commands and can inject prompts, focus Codex, and send Escape via AppleScript
- temporary public-tunnel remote trial flow with automatic fallback
- API smoke test for session, command queue, and WebSocket relay flows
- macOS desktop launcher that bundles the local web app into a self-contained `.app`

Not done yet:

- signed/notarized macOS distribution
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
   - can run either from source or from the packaged Mac app
   - captures the full screen with `getDisplayMedia`
   - displays one QR code and one token-protected phone viewer link
   - shows host and phone connection state in a simplified centered UI
   - keeps using `localhost` for the Mac-side page even when a remote trial tunnel is active
   - streams the media to the phone browser using WebRTC

3. Mac agent
   - runs locally on the Mac
   - polls the FastAPI service for queued commands
   - authenticates using the session token
   - activates the `Codex` app
   - supports native `focus` and `stop` style control actions from the phone viewer
   - replaces the existing draft in the focused Codex prompt field before pasting
   - optionally presses Return to submit
   - can be auto-started by the host page for the active session
   - can be relaunched by the packaged app without requiring Python on the user's machine

## Repo Layout

- `shared-backend/app/main.py`: FastAPI app, session store, command queue, signaling server
- `shared-backend/mac_agent.py`: Mac-side Codex prompt injector
- `shared-backend/pocketcodex_desktop.py`: packaged desktop launcher entrypoint
- `shared-backend/web/host.html`: standalone host page used on the Mac for direct/debug entry
- `shared-backend/web/viewer.html`: mobile viewer/control page
- `shared-backend/web/index.html`: one-click Mac host flow
- `shared-backend/scripts/smoke_test.py`: local API smoke test
- `shared-backend/scripts/fake_tunnel.py`: deterministic test helper for the remote trial flow
- `packaging/build_macos_app.sh`: builds `dist/PocketMac.app` with PyInstaller

## macOS App Packaging

Pocket Mac can now be built as a self-contained macOS app bundle so end users do not need their
own Python installation.

What the packaged app does:

- bundles the Python runtime and Pocket Mac backend
- bundles `cloudflared` for public remote links
- bundles a Node runtime plus `localtunnel` as a second remote fallback
- stores writable app data under `~/Library/Application Support/PocketMac`
- launches the local server itself
- opens the local host UI in the browser
- can relaunch the local Mac agent from the same bundled runtime

Build the `.app` locally:

```bash
./packaging/build_macos_app.sh
```

That produces:

```bash
dist/PocketMac.app
dist/PocketMac.dmg
```

The build script was verified locally by:

- building `dist/PocketMac.app`
- building `dist/PocketMac.dmg`
- launching the bundled executable directly
- hitting `/api/health` successfully from the built app runtime

Current limitation:

- the app bundle is built and runnable locally, but it is not yet code signed or notarized for public distribution

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

- put Pocket Mac behind HTTPS at `PUBLIC_BASE_URL`
- keep the generated tokenized links private
- configure your own TURN server in `ICE_SERVERS_JSON`
- prefer a dedicated small VPS or Cloudflare/Tailscale-style entry point over exposing a raw home IP

For quick public testing without running your own infra, Pocket Mac now has a temporary remote trial mode:

- it starts a public tunnel to the local FastAPI service
- it keeps the Mac host page on `localhost`
- it switches the adaptive phone viewer link and QR code to the public tunnel URL
- it still shows a separate same-Wi-Fi viewer link and QR code

The current implementation tries tunnel providers in this order:

- bundled `cloudflared`
- bundled `localtunnel`
- same-Wi-Fi LAN fallback when both remote providers are unavailable

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

The streamlined launch/host flow can now start the local Mac agent automatically for the active session.
The manual command above is still useful as a fallback and for debugging.

### 3. Open Pocket Mac on the Mac

Open:

- `http://127.0.0.1:8000/`

The launch page now owns the Mac host flow directly:

- one start button on the Mac
- session creation happens behind the scenes
- the same click starts local session setup, tunnel selection, agent startup, and the Safari screen-share request
- the same page then shows the single phone link, single QR code, and phone connection status
- once active, the button changes state instead of suggesting another manual share step

The Mac-side UI intentionally exposes only one path:

- `Stream Your Mac Controls to Your Phone`
- one phone viewer link
- one QR code
- one phone connection status indicator
- the same first click also starts the local Mac agent for that session

Under the hood, the host flow now requests entire-screen sharing only. Safari still controls the
final picker, so you will be asked to choose the full screen when sharing begins.

If you need a direct/debug entry, `host.html` still exists, but the normal product flow should start from `/`.

### 4. Open the viewer page on the phone

Use the generated viewer link from the launch page.

If you are testing outside your local network, put the server behind a secure tunnel or relay.

The viewer is now a hybrid remote console:

- the live stream stays front and center
- only one viewer at a time can `Take Control`
- native buttons on the phone can `Send`, `Paste Draft`, `Focus`, and `Stop`
- additional viewers can still watch, but they stay read-only until they take control

### 5. Remote Trial

The one-click Mac flow now starts the remote phone path automatically during preparation.

That does three things:

- starts a temporary public tunnel to the local Pocket Mac server when available
- updates the phone viewer link and QR code to the public tunnel URL
- falls back to a same-Wi-Fi link when public tunnel providers are unavailable

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
- Safari screen sharing works when the Mac page is opened on `localhost` or HTTPS.
- The launch page now doubles as the host flow, so the first click handles session setup, tunnel selection, agent startup, and the browser screen-share prompt in one place.
- The standalone host page is still present for debugging, but the main UI path is `/`.
- Prompt injection now replaces the existing Codex draft by default, which avoids accidental prompt concatenation during phone control.
- The viewer page now shows recent command results, controller status, and whether the host and agent appear online.
- Set `PUBLIC_BASE_URL` to a public HTTPS URL if you want QR codes that open correctly off-network.
- The current host flow prefers a public remote-trial phone link and only falls back internally when a tunnel provider is unavailable.
- If every public tunnel provider fails, the Mac page shows a same-network-only phone link instead of a broken remote link.
- For reliable cross-network streaming, configure a TURN server in `ICE_SERVERS_JSON`.
- Reusing a session now means reopening its original signed host/viewer link, not recreating the session by id.

## Near-Term Roadmap

- move from polling to a persistent control socket for the Mac agent
- add basic remote click targets for common Codex actions
- add a small Swift menu-bar app so the Mac side is one install instead of a browser page plus script
- bundle a hosted relay path so the public URL and TURN config work without manual env setup
