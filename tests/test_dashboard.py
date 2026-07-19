"""Tests for the internal dashboard page.

The dashboard router is not wired into ``app.main`` yet (the orchestrator does
that later), so these tests stand up a LOCAL FastAPI app carrying only
``dashboard.router`` over an isolated in-memory database, rather than leaning on
conftest's shared ``client`` fixture (which boots the real app).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.models import Base, Client, PlotCheck
from app.models.enums import ClientSide, RiskLevel
from app.routers import dashboard


@pytest.fixture
def _engine():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        cur = dbapi.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def dash_client(_engine):
    factory = sessionmaker(bind=_engine, expire_on_commit=False)

    def _override():
        with factory() as s:
            yield s

    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c, factory


def _make_plot_check(token: str, risk: RiskLevel, area_ha: float) -> PlotCheck:
    """A minimal, valid PlotCheck.

    ``geometry_geojson`` and ``geometry_type`` are NOT NULL on the model, so
    both are always set; ``risk_level`` and ``area_ha`` drive the assertions.
    """
    return PlotCheck(
        token=token,
        source_format="geojson",
        geometry_geojson='{"type": "Point", "coordinates": [0, 0]}',
        geometry_type="Point",
        area_ha=area_ha,
        risk_level=risk,
        result_json="{}",
    )


# --------------------------------------------------------------------------- #
# Empty database                                                                #
# --------------------------------------------------------------------------- #
def test_empty_dashboard_renders_zeros_and_empty_state(dash_client) -> None:
    client, _factory = dash_client

    resp = client.get("/dashboard")
    assert resp.status_code == 200

    body = resp.text
    assert "Dashboard" in body
    # No plot checks yet -> the empty-state copy, not a table row.
    assert "No plot checks yet." in body
    # Honesty captions are always present.
    assert "not yet integrated" in body
    # Quick links to every destination are present.
    for href in ("/plot-checker", "/scope-checker", "/clients", "/dds", "/dashboard"):
        assert f'href="{href}"' in body


# --------------------------------------------------------------------------- #
# Populated database                                                            #
# --------------------------------------------------------------------------- #
def test_counts_and_recent_reflect_rows(dash_client) -> None:
    client, factory = dash_client

    with factory() as s:
        s.add_all(
            [
                Client(name="Acme Coffee", side=ClientSide.importer_eu),
                Client(name="Selva Cocoa", side=ClientSide.exporter_sa),
            ]
        )
        s.add_all(
            [
                _make_plot_check("tokenAAAAAAAAAAAAAAA", RiskLevel.green, 1.2345),
                _make_plot_check("tokenBBBBBBBBBBBBBBB", RiskLevel.red, 6.7890),
                _make_plot_check("tokenCCCCCCCCCCCCCCC", RiskLevel.red, 3.0000),
            ]
        )
        s.commit()

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.text

    # Names are not rendered (only counts are), so a name would be a leak/bug.
    assert "Acme Coffee" not in body
    # The emerald stat-card value carries the raw count: two clients.
    assert ">2</p>" in body
    # Three plot checks, two of them red (the red tally sits next to " red").
    assert "Plot checks" in body
    assert ">3</p>" in body
    assert ">2</span> red" in body

    # Recent list shows the tokens (truncated to 12 chars) and formatted areas.
    assert "tokenBBBBBBB" in body
    assert "6.7890" in body
    # The empty-state copy is gone once there are rows.
    assert "No plot checks yet." not in body


def test_recent_capped_at_five_most_recent(dash_client) -> None:
    client, factory = dash_client

    # Tokens differ within their first 12 chars (the display truncation) and
    # carry explicit, strictly increasing created_at so "5 most recent" is
    # deterministic. "checkNN..." -> displayed as "checkNN" + padding.
    with factory() as s:
        for i in range(7):
            check = _make_plot_check(
                f"check{i:02d}xxxxxxxxxxxxxxxxxxxxx"[:43],
                RiskLevel.green,
                float(i),
            )
            check.created_at = datetime(2026, 1, 1, 0, 0, i, tzinfo=UTC)
            s.add(check)
        s.commit()

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.text

    # 7 inserted, only the 5 newest (indices 2..6) render; the oldest two drop.
    for i in (2, 3, 4, 5, 6):
        assert f"check{i:02d}" in body
    for i in (0, 1):
        assert f"check{i:02d}" not in body


def test_recent_row_with_null_risk_and_area_renders(dash_client) -> None:
    """A check whose risk_level and area_ha are NULL (both nullable) must not
    break the template: it renders an "n/a" badge and an em-dash area."""
    client, factory = dash_client

    with factory() as s:
        s.add(
            PlotCheck(
                token="nulltokenXXXXXXXXXXX",
                source_format="geojson",
                geometry_geojson='{"type": "Point", "coordinates": [0, 0]}',
                geometry_type="Point",
            )
        )
        s.commit()

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.text
    assert "nulltokenXXX" in body  # token[:12]
    assert "n/a" in body  # NULL risk badge label
    assert "—" in body  # NULL area placeholder
