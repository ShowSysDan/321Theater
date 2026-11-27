# 3·2·1→Theater AI node

A dedicated box (a GPU server) that runs the AI model for every 321Theater app
server, **without database access**.

```
 app servers (claim jobs from PostgreSQL, build prompts, store results)
      │  HTTPS, certificate pinned by fingerprint, every request signed
      ▼
 AI node: ai_node.py ──► Ollama on 127.0.0.1 (no password, never on the network)
```

- The app servers do all the database work: they claim AI jobs from the queue,
  build the prompt (already scrubbed of names, contacts and money), and write
  the results. The node only gets one model call at a time and keeps nothing.
- The node has no database login, no S3 keys and no 321Theater app code. It's
  one standard-library Python file (`ai_node.py`).
- Only four routes, mirroring the parts of Ollama's API the app uses:
  `GET /api/version`, `GET /api/tags`, `POST /api/chat`, `POST /api/pull`.
  Ollama's delete / create / copy / push are not reachable. Chat requests are
  rebuilt from an allow-list (roles, text, JSON format, clamped options).
- Security on the LAN:
  - HTTPS with a self-signed certificate. The app servers **pin** its SHA-256
    fingerprint and check it before sending anything, so notes and comments
    never go to a box that isn't this one.
  - Every request is signed with a shared secret (HMAC-SHA256), within a
    ±60 s clock window, and each request can be used only once.
  - `NODE_ALLOW_IPS`: only the app servers' addresses may connect, at most 8
    connections each, and a connection that hasn't proven it has the secret
    within 15 seconds is closed.
  - The app says (signed) how long it will wait; the node never holds a call
    longer, and stops the model as soon as the app hangs up.
  - The node's log has method, path, status, time, IP and an error code, never
    prompt or answer text.

## Install (on the AI box, Debian / Ubuntu, Python 3.8+)

```bash
sudo git clone --filter=blob:none --sparse https://github.com/ShowSysDan/321Theater /opt/321ai-node-src
cd /opt/321ai-node-src && sudo git sparse-checkout set ai_node
sudo bash ai_node/install.sh --with-ollama
```

Then edit `/etc/321ai-node/node.env`:

- `NODE_ALLOW_IPS` (**required**, the node won't start without it): the app
  servers' IPs, e.g. `10.201.2.101,10.201.2.102,10.201.2.103`
- `NODE_MODELS`: the model(s) the app uses, e.g. `llama3.1:8b`. **Pull model**
  in Settings → AI only works for models listed here.

```bash
sudo systemctl restart 321ai-node
```

The install prints three lines. Put them in the `.env` of every app server
that should use the node (with this box's LAN address), then restart that app:

```
AI_NODE_URL=https://10.201.2.150:11500
AI_NODE_SECRET=…
AI_NODE_CERT_SHA256=…
```

Settings → AI → Servers then shows those servers "via AI node". **Test this
server** checks the connection end to end. Set the model in Settings → AI (or
`OLLAMA_MODEL` per server) and press **Pull model** once.

## Update

```bash
cd /opt/321ai-node-src && sudo git pull && sudo bash ai_node/install.sh
```

The update never overwrites `node.env` or the certificate.

## Errors you may see in Settings → AI

| Code | Meaning |
| --- | --- |
| `cert_mismatch` | `AI_NODE_CERT_SHA256` doesn't match the node's certificate. Nothing was sent. |
| `node_unauthorized` | `AI_NODE_SECRET` differs from `NODE_SECRET`, or the clocks differ by more than a minute (check NTP). |
| `node_model_not_allowed` | The model isn't in `NODE_MODELS`. |
| `ollama_unreachable` | The node is up but its Ollama isn't (`systemctl status ollama`). |
| `node_busy` | All `NODE_MAX_CONCURRENT` slots stayed busy for `NODE_QUEUE_S` seconds. |
| `unreachable` | No connection: node down, wrong address / port, firewall, or this IP isn't in `NODE_ALLOW_IPS`. |

To rotate the certificate: delete `/etc/321ai-node/node.crt` and `node.key`,
re-run `install.sh`, and put the new fingerprint on every app server.
To rotate the secret: change `NODE_SECRET` and every app server's
`AI_NODE_SECRET`, then restart both.
