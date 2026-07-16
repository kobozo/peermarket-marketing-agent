"""Aggregate-only PeerMarket attribution reads."""

from datetime import date

from peermarket_agent.mcp_servers.peermarket_readonly import PeermarketReadonly


class _Result:
    def mappings(self):
        return self

    def all(self):
        return [
            {
                "day": date(2026, 7, 15),
                "utm_source": "meta",
                "utm_medium": "paid-social",
                "utm_campaign": "summer",
                "utm_content": "draft-156",
                "event_type": "registration",
                "event_count": 2,
            }
        ]


class _Connection:
    def __init__(self):
        self.executed_sql = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, statement, params):
        self.executed_sql = str(statement)
        self.params = params
        return _Result()


class _Engine:
    def __init__(self):
        self.connection = _Connection()

    def connect(self):
        return self.connection


async def test_attribution_reader_queries_only_aggregate_view():
    readonly = object.__new__(PeermarketReadonly)
    readonly._engine = _Engine()

    rows = await readonly.fetch_attribution(date(2026, 7, 15), date(2026, 7, 16))

    sql = readonly._engine.connection.executed_sql
    assert "marketing_attribution_daily" in sql
    assert "campaign_touches" not in sql
    assert "campaign_events" not in sql
    assert rows[0].event_count == 2
    assert readonly._engine.connection.params == {
        "start": date(2026, 7, 15),
        "stop": date(2026, 7, 16),
    }
