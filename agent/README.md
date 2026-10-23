# OdooBridger Agent

Lets OdooBridger reach an Odoo that is only reachable inside a customer's own
network: an on-premise server, a machine behind NAT, anything without a public
address. No port forwarding, no VPN, no inbound firewall rule.

## How it works

```
 Customer's network                         OdooBridger (cloud)
 ┌──────────────────────────┐   outgoing    ┌───────────────────────────────┐
 │ Odoo 192.168.1.20:8069   │   WebSocket   │ core: relay hub               │
 │        ▲                 │  (wss, 443)   │   /relay/v1/agent             │
 │        │ local HTTP      │ ────────────▶ │   ▲            ▲              │
 │ OdooBridger Agent ───────┼───────────────┼───┘            │              │
 └──────────────────────────┘               │ connection   POST /api/v1/    │
                                            │ test          relay/forward   │
                                            │                 ▲             │
                                            │ BioBridge (and other services)│
                                            └───────────────────────────────┘
```

1. The agent dials out to `wss://<core>/relay/v1/agent` with its key and says
   which addresses it may reach (`OB_ALLOW`).
2. The core's connection test, and any service syncing with that Odoo, hand
   their HTTP requests to the core's relay. Services authenticate with their
   client id and secret and may only use the connection their account bound
   them to (`POST /api/v1/relay/forward`).
3. The hub sends each request down the agent's socket; the agent makes it on
   the local network and returns the answer. Many requests share the socket.

Odoo's own API key still travels inside each request, exactly as it would on a
direct connection: the agent adds no access of its own.

## Safety

* **Only the listed addresses.** A request for any origin not in `OB_ALLOW` is
  refused by the agent itself, whatever the server asks. The core also refuses
  to send one.
* **No redirects followed**, so an allowed address can't bounce a request
  somewhere else.
* **Keys** start with `obag_`, are shown once, are stored hashed, and can be
  replaced or revoked from the portal (the agent is disconnected at once).
* Outgoing only; TLS to OdooBridger. `OB_TLS_VERIFY=false` relaxes only the
  check of the **local** Odoo's certificate (self-signed internal servers).

## Running it

From the portal: **Odoo → OdooBridger Agents → Add Agent**. It shows the key
and the commands with your values filled in.

Docker (build the image once from this folder, or use the image you publish):

```bash
docker build -t odoobridger-agent .
docker run -d --name odoobridger-agent --restart unless-stopped \
  -e OB_AGENT_KEY=obag_... \
  -e OB_RELAY_URL=wss://app.odoobridger.com/relay/v1/agent \
  -e OB_ALLOW=http://192.168.1.20:8069 \
  odoobridger-agent
```

Python 3.10+ (the script is also served by the core at `/downloads/odoobridger_agent.py`):

```bash
pip install websockets httpx
OB_AGENT_KEY=obag_... OB_RELAY_URL=wss://... OB_ALLOW=http://192.168.1.20:8069 \
  python3 odoobridger_agent.py
```

`python3 odoobridger_agent.py --check` tests that each `OB_ALLOW` address
answers from this machine, and exits.

| Setting | |
|---|---|
| `OB_AGENT_KEY` | the key from the portal |
| `OB_RELAY_URL` | `wss://<core address>/relay/v1/agent` |
| `OB_ALLOW` | comma-separated origins it may reach, e.g. `http://192.168.1.20:8069` |
| `OB_TLS_VERIFY` | `false` to accept a self-signed certificate on the local Odoo |
| `OB_CONCURRENCY` | requests at once (default 8) |
| `OB_LOG_LEVEL` | `INFO` or `DEBUG` (DEBUG logs each request's method and URL, never bodies) |

It reconnects on its own with back-off; a refused key is retried every 5 minutes.

## Limits and what's next

* Requests and answers up to 10 MB; 60 s per request by default.
* The hub keeps sockets in the core's process, so the core runs as one process
  (it does). Running several core instances needs a shared router (e.g. Redis).
* Built for Odoo; the protocol is plain HTTP, so LAN-only biometric terminals
  can use the same agent later by adding their address to `OB_ALLOW` and giving
  BioBridge's device providers the same relay transport.

The copy at `core/obcore/static/downloads/odoobridger_agent.py` must match this
one (a core test checks it).
