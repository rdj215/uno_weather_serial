"""Shared test doubles. No test here needs a real Postgres, broker or serial port."""

import pytest


class FakeCursor:
    """Records every execute() call; rowcount says whether the last INSERT 'happened'."""

    def __init__(self, rowcount=1):
        self.calls = []
        self.rowcount = rowcount

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def sql_for(self, table):
        return [(sql, params) for sql, params in self.calls if f"INSERT INTO {table}" in sql]


class FakeStore:
    """Stands in for mqtt_to_postgres.Store: runs fn against a FakeCursor."""

    def __init__(self, rowcount=1):
        self.cur = FakeCursor(rowcount)

    def run(self, fn):
        return fn(self.cur)


@pytest.fixture
def cur():
    return FakeCursor()


@pytest.fixture
def store():
    return FakeStore()
