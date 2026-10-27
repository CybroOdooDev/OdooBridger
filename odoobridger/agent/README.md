# OdooBridger Agent

Lets OdooBridger reach what is only reachable inside a customer's own network:
an on-premise Odoo, biometric terminals, a machine behind NAT. No port
forwarding, no VPN, no inbound firewall rule.

**The customer installs it once, with only its key.** Everything after that is
managed in the OdooBridger portal and applied to the running agent at once:
connecting an Odoo or a biometric device through the agent adds the address it
needs, and addresses can be added or removed on the Odoo page. Nothing on the
customer's computer is ever edited or restarted.

## How it works

```
 Customer's network                         OdooBridger (cloud)
 ┌──────────────────────────┐   outgoing    ┌───────────────────────────────┐
 │ Odoo 192.168.1.20:8069   │   WebSocket   │ core: relay hub               │
 │ uFace 10.0.11.43:4370    │  (wss, 443)   │   /relay/v1/agent             │
 │        ▲                 │ ────────────▶ │   ▲            ▲              │
 │ OdooBridger Agent ───────┼───────────────┼───┘            │              │
 └──────────────────────────┘               │ connection   relay/forward,   │
                                            │ test          relay/stream    │
                                            │ BioBridge (and other services)│
                                            └───────────────────────────────┘
```

1. The agent dials out to `wss://<core>/relay/v1/agent` with its key. The core
   answers with the addresses managed for it, and sends a new list whenever
   they change (`config` message).
2. Web requests (Odoo, Hikvision, Dahua, COSEC...) are carried one by one;
   a terminal's own TCP protocol (standalone ZKTeco, port 4370) is carried as
   a stream.
3. Services authenticate with their client id and secret and may only use
   their own account's agent (`POST /api/v1/relay/forward`,
   `WS /api/v1/relay/stream`, `POST /api/v1/relay/agents/{id}/addresses`).

Odoo's own API key and a terminal's comm key still travel inside the traffic:
the agent adds no access of its own.

## What it may reach

Three checks, the last two made by the agent itself whatever OdooBridger asks:

1. **Managed in OdooBridger**: `host:port` entries (or ranges like
   `192.168.1.0/24@80+443`), added automatically or by hand on the Odoo page.
2. **Your own network only**: with no `OB_ALLOW` set, only private addresses
   (10.x, 172.16-31.x, 192.168.x, 100.64.x, this computer) are reached; a name
   is resolved and must point inside the network.
3. **Optional local limit**: if the customer's IT sets `OB_ALLOW` on the
   computer, the agent also keeps to that.

No redirects are followed. Keys start with `obag_`, are shown once, stored
hashed, and can be replaced or revoked from the portal (the agent is
disconnected at once).

When an agent comes online, the core re-tests the Odoo connections that go
through it and asks each service to check again, so a connection that failed
while the agent was off recovers without anyone saving it again.

## Running it

From the portal: **Odoo → Private Network** (guided), or **OdooBridger Agents →
Add Agent**. It shows the key and the command with your values filled in.

Docker, no image to build (the agent is downloaded from your OdooBridger at
each start, so a restart also updates it):

```bash
docker run -d --name odoobridger-agent --restart unless-stopped --network host \
  -e OB_AGENT_KEY=obag_... \
  -e OB_RELAY_URL=wss://app.yourdomain.com/relay/v1/agent \
  python:3.12-slim sh -c "pip install -q websockets httpx && python -c \"import urllib.request as u; u.urlretrieve('https://app.yourdomain.com/downloads/odoobridger_agent.py','/a.py')\" && python /a.py"
```

On Docker Desktop (Windows, Mac) leave out `--network host`. Python 3.10+:

```bash
pip install websockets httpx
curl -O https://app.yourdomain.com/downloads/odoobridger_agent.py
OB_AGENT_KEY=obag_... OB_RELAY_URL=wss://app.yourdomain.com/relay/v1/agent python3 odoobridger_agent.py
```

| Setting | |
|---|---|
| `OB_AGENT_KEY` | the key from the portal (required) |
| `OB_RELAY_URL` | `wss://<core address>/relay/v1/agent` (required; `ws://` for a local test) |
| `OB_ALLOW` | optional local limit, comma separated: `192.168.1.20:8069`, `http://192.168.1.20:8069`, `192.168.1.0/24@80+443+4370` |
| `OB_TLS_VERIFY` | `false` to accept a self-signed certificate on the local Odoo |
| `OB_CONCURRENCY` | web requests at once (default 8) |
| `OB_LOG_LEVEL` | `INFO` or `DEBUG` (DEBUG logs each request's method and URL, never bodies) |

It reconnects on its own with back-off; a refused key is retried every 5
minutes. Version 1.0 agents (which only knew `OB_ALLOW`) keep working; the
portal marks them **Update Needed** until restarted.

## Limits

* Requests and answers up to 10 MB; 60 s per request by default. Device
  streams: 4 per account at once, 120 s idle, 15 minutes at most.
* The hub keeps sockets in the core's process, so the core runs as one process.
  Running several core instances needs a shared router (e.g. Redis).
* Push devices (ZKTeco ADMS) dial out themselves and don't use the agent.

The copy at `core/obcore/static/downloads/odoobridger_agent.py` must match this
one (a core test checks it).
