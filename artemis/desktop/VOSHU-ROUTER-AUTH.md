# Voshu Router — authorized internal access for services

Canonical pattern for connecting an internal service (ARTEMIS, agents, jobs)
to Voshu Router inference. Established 2026-11-27; replaces the prototype
file-surgery key, which has been revoked.

## The trust model

Voshu Router (CT 171, containerized, `100.98.33.52:8787`) has two tiers:

- **Operator tier** — loopback on the router host, or `X-Voshu-Admin-Token`.
  Operator-only routes live under `/api/inference/*`: mint/list/revoke keys,
  accounts, grants, usage. This is the key-management plane.
- **Member tier** — `Authorization: Bearer sk-mem-*` on `/v1/*` inference
  endpoints. This is the data plane services actually use.

Internal-service access therefore means: an operator mints a scoped member
key once; the service thereafter needs only its member key and never
touches operator credentials.

## Minting a service key (the sanctioned path)

From the router host (pve-leh: `pct exec 171 -- bash`):

```
curl -X POST http://127.0.0.1:8787/api/inference/keys \
  -H "Content-Type: application/json" \
  -d '{"memberId":"svc-<name>","label":"<what/where>","projectIds":["<project>"],"agentIds":["<agent>"]}'
```

Rules:
- `memberId` must match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`.
- Internal services use the `svc-*` convention (existing example:
  `svc-paperclip`). People use their names (`alex`, `chad`).
- The plaintext key is shown ONCE in the response. Pipe it directly into
  its target file — never echo it to logs or chat.
- Every mint/revoke is audited in `inference-audit.jsonl` with principal
  attribution — file surgery on `inference-keys.json` bypasses audit and
  is forbidden now that this path is documented.

## Service configuration

```
OPENAI_BASE_URL=http://100.98.33.52:8787/v1
OPENAI_API_KEY=<the sk-mem key>
```

OpenAI-compatible. Model inventory: `GET /v1/models` with the key.
As of 2026-11-27: 71 models, 21 vision-capable (incl. gemini flash variants).

## ARTEMIS's key (current)

- keyId `key_398424200a3969c0`, member `svc-artemis`,
  agentIds `[planner, agy-flash]` (scoped whitelist),
  label `artemis-cua-windows-fleet`, project `voshu-alex`
- Lives at: `~/.artemis-router-key` on the Mac Studio (mode 600) and
  `/root/.svc-artemis-key` inside CT 171 (backup copy, mode 600)
- Revoked predecessors: `key_e6526704976abfa7` (file surgery),
  `key_ceddde995d350460` (wrong memberId), `key_76678e602493ae70`
  (unscoped agent list) — all verified dead.
- Rotation: mint a new key, update the two files + the artemis runtime
  config, revoke the old keyId via `POST /api/inference/keys/<id>/revoke`.

## Verified status (2026-11-27)

- Text inference PROVEN end-to-end: raw HTTP and the production
  langchain-openai chain both round-trip through `agent/planner`.
- Scope model: `agent/*` models check the key's agentIds whitelist; empty
  means unrestricted. `kilo/*` and other non-agent models need no agent scope.
- Vision inference currently UNAVAILABLE through the router: antigravity
  upstream binary missing, kilo free VL pool rate-limited, `vision/*` NIM
  upstream down, all agent/* models text-only. ARTEMIS's operator loop is
  element-tree-primary so text suffices for basic operation, but
  screenshot-evidence and video verification need a vision upstream restored
  (router-side infrastructure task, CT 171).
- MCP wiring: OPENAI_BASE_URL + OPENAI_API_KEY live in the windsurf
  mcp_config.json artemis env block (the dotenv path is hook-blocked for
  agents; the MCP env block is the documented alternative and preferred
  since the server reads env at process start).
