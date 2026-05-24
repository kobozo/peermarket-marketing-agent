# peermarket-marketing-agent

Self-extending marketing agent for PeerMarket. Runs on Proxmox VM 129 (`agent-peermarket`).

See spec: `kobozo/secondhand → docs/superpowers/specs/2026-05-23-marketing-agent-design.md`.

## Local dev

```bash
uv sync --all-extras
docker run --rm -d --name agent-test-db -p 55432:5432 \
  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=agent_test \
  pgvector/pgvector:pg15
export AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test
uv run pytest -v
```
