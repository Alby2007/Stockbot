"""Card face rendering: PNG output, determinism, and the frame → border
cache key. Pure-function renders -- no DB needed."""

from __future__ import annotations

import pytest

from stockbot.bot.cardart import (
    FaceSpec,
    render_card_face,
    render_card_spread,
)


def _spec(**kw: object) -> FaceSpec:
    base = dict(
        card_key="card_nort",
        name="Northlight Freight",
        kind="INSTRUMENT",
        frame="STANDARD",
        serial=7,
        stamps=(),
        set_label="base",
        flavor="Rail freight.",
        sector_name="Industrials",
    )
    base.update(kw)
    return FaceSpec(**base)  # type: ignore[arg-type]


async def test_face_renders_png() -> None:
    buf = await render_card_face(_spec())
    data = buf.read()
    assert data[:4] == b"\x89PNG"
    assert len(data) > 5_000


async def test_face_deterministic_and_cached() -> None:
    a = await render_card_face(_spec())
    b = await render_card_face(_spec())  # cache-hit path
    assert a.read() == b.read()


async def test_face_differs_across_identity() -> None:
    std = await render_card_face(_spec())
    leg = await render_card_face(_spec(frame="LEGENDARY"))
    other_serial = await render_card_face(_spec(serial=8))
    assert std.read() != leg.read()
    # Serial is part of the face -- mint numbers must not collide.
    assert std.read() != other_serial.read()


@pytest.mark.parametrize("kind", ["INSTRUMENT", "LORE", "COMMEMORATIVE", "PART", "ASSEMBLED"])
async def test_all_kinds_render(kind: str) -> None:
    frame = "PART" if kind == "PART" else "GOLD"
    buf = await render_card_face(_spec(kind=kind, frame=frame, serial=None))
    assert buf.read()[:4] == b"\x89PNG"


async def test_spread_renders_multi() -> None:
    specs = [_spec(), _spec(frame="GOLD", serial=3, stamps=("MOON",))]
    buf = await render_card_spread(specs)
    assert buf.read()[:4] == b"\x89PNG"
