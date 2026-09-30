"""Collectible card faces: matplotlib -> PNG, same conventions as
charts.py (Agg backend, Figure not pyplot, sync render wrapped in
asyncio.to_thread, LRU byte cache).

A face is a pure function of (card_key, frame, serial, stamps, edition)
so the fingerprint IS the cache key -- the serial lives in it because
each mint number is a different card. The art window is procedural:
seeded from sha256(card_key), every card in the set gets a distinct but
reproducible motif (no per-card authored art to maintain).

matplotlib can't render emoji, so provenance stamps draw as text chips
and tiers as border geometry instead of the FRAME_GLYPH emoji set.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import random
import textwrap
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.colors as mcolors  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import Circle, FancyBboxPatch, Rectangle  # noqa: E402

from stockbot.bot.collection_view import FRAME_COLOR  # noqa: E402

# Face proportions: 3.5x5 in at 120dpi = 420x600 px -- trading-card ratio.
_FACE_W, _FACE_H = 3.5, 5.0
_DPI = 120

_BG = "#10151d"
_PANEL = "#161c27"
_TEXT = "#e8eaf0"
_MUTED = "#8b93a3"

# Provenance stamps draw as labeled chips (emoji won't rasterize).
_STAMP_CHIP = {
    "HALT_PRINT": "HALT",
    "PEAK_PRINT": "PEAK",
    "DAY_ONE": "DAY 1",
    "OFF_HOURS": "AH",
    "MOON": "MOON",
    "CRATER": "CRATER",
    "PITY_BREAK": "PITY",
}

# Per-kind art motifs; INSTRUMENT uses the sector hash for hue so a
# sector's cards share a color family.
_KIND_MOTIF = {
    "INSTRUMENT": "candles",
    "LORE": "sigil",
    "COMMEMORATIVE": "burst",
    "PART": "blueprint",
    "ASSEMBLED": "seal",
}

_FACE_CACHE: OrderedDict[tuple[Any, ...], bytes] = OrderedDict()
_FACE_CACHE_MAX = 128


@dataclass(frozen=True)
class FaceSpec:
    """Everything a card face needs -- one of these per reveal outcome."""

    card_key: str
    name: str
    kind: str  # INSTRUMENT | LORE | COMMEMORATIVE | PART | ASSEMBLED
    frame: str  # STANDARD..LEGENDARY | PART
    serial: int | None = None
    stamps: tuple[str, ...] = ()
    set_label: str = ""
    flavor: str = ""
    sector_name: str | None = None
    first_edition: bool = False


def _frame_hex(frame: str) -> str:
    return f"#{FRAME_COLOR.get(frame, 0x95A5A6):06x}"


def _seed(key: str) -> int:
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")


def _art_hue(spec: FaceSpec, rng: random.Random) -> tuple[float, float, float]:
    """Deterministic hue: sector family for instruments, stable per-key
    hue otherwise, saturated mid-range so faces read as art not noise."""
    if spec.kind == "INSTRUMENT" and spec.sector_name:
        base = _seed(f"sector:{spec.sector_name}") % 360 / 360
        return (base, 0.55, 0.75)
    base = _seed(spec.card_key) % 360 / 360
    return (base, 0.50, 0.70)


def _draw_motif(ax: Any, spec: FaceSpec, rng: random.Random) -> None:
    """The procedural art window. Each kind gets a motif vocabulary;
    the sha256 seed picks arrangement so a card's face never changes."""
    hue, sat, val = _art_hue(spec, rng)
    accent = mcolors.hsv_to_rgb((hue, sat, val))
    accent2 = mcolors.hsv_to_rgb(((hue + 0.13) % 1.0, sat * 0.8, val * 0.85))
    motif = _KIND_MOTIF.get(spec.kind, "candles")

    if motif == "candles":
        # Mini candlestick field -- the market itself as the art.
        n = 9
        for i in range(n):
            x = 0.08 + i * (0.84 / n)
            o = rng.uniform(0.15, 0.75)
            c = o + rng.uniform(-0.18, 0.18)
            h = max(o, c) + rng.uniform(0.02, 0.10)
            lo = min(o, c) - rng.uniform(0.02, 0.10)
            col = accent if c >= o else accent2
            ax.plot([x, x], [lo, h], color=col, lw=1.2, alpha=0.9)
            ax.add_patch(
                Rectangle(
                    (x - 0.015, min(o, c)), 0.03, abs(c - o) or 0.01,
                    facecolor=col, edgecolor="none", alpha=0.95,
                )
            )
    elif motif == "sigil":
        # Concentric offset rings + a core disc: a portrait-token feel.
        for i in range(4):
            r = 0.36 - i * 0.075
            ax.add_patch(
                Circle(
                    (0.5 + rng.uniform(-0.03, 0.03), 0.5 + rng.uniform(-0.03, 0.03)),
                    r, fill=False, edgecolor=accent if i % 2 == 0 else accent2,
                    lw=1.6, alpha=0.85,
                )
            )
        ax.add_patch(Circle((0.5, 0.5), 0.10, facecolor=accent, alpha=0.9))
    elif motif == "burst":
        # Radial rays from center -- commemoratives are event flashes.
        for i in range(16):
            a = i * math.pi / 8 + rng.uniform(-0.06, 0.06)
            r0, r1 = 0.10, rng.uniform(0.28, 0.45)
            ax.plot(
                [0.5 + r0 * math.cos(a), 0.5 + r1 * math.cos(a)],
                [0.5 + r0 * math.sin(a), 0.5 + r1 * math.sin(a)],
                color=accent if i % 2 else accent2, lw=1.8, alpha=0.9,
            )
        ax.add_patch(
            Circle((0.5, 0.5), 0.07, facecolor=_TEXT, edgecolor=accent, lw=1.5)
        )
    elif motif == "blueprint":
        # Grid + dashed construction circles: parts look drafted.
        for i in range(5):
            v = 0.12 + i * 0.19
            ax.axhline(v, color=accent, lw=0.5, alpha=0.35)
            ax.axvline(v, color=accent, lw=0.5, alpha=0.35)
        for _ in range(3):
            ax.add_patch(
                Circle(
                    (rng.uniform(0.3, 0.7), rng.uniform(0.3, 0.7)),
                    rng.uniform(0.08, 0.2), fill=False, edgecolor=accent2,
                    lw=1.0, ls=(0, (4, 3)), alpha=0.9,
                )
            )
        ax.plot([0.5, 0.62, 0.62, 0.5], [0.35, 0.35, 0.55, 0.35], color=accent, lw=2)
    else:  # seal -- assembled prestige pieces
        ax.add_patch(
            Circle((0.5, 0.5), 0.34, fill=False, edgecolor=accent, lw=3)
        )
        ax.add_patch(
            Circle((0.5, 0.5), 0.27, fill=False, edgecolor=accent2, lw=1.2)
        )
        ax.add_patch(Circle((0.5, 0.5), 0.12, facecolor=accent, alpha=0.9))
        ax.plot(
            [0.42, 0.42, 0.5], [0.16, 0.05, 0.10], color=accent, lw=2
        )
        ax.plot(
            [0.58, 0.58, 0.5], [0.16, 0.05, 0.10], color=accent, lw=2
        )


def _draw_face(ax: Any, spec: FaceSpec) -> None:
    """Draw one card face into a 0..1 x 0..1 axes."""
    rng = random.Random(_seed(spec.card_key))
    frame_col = _frame_hex(spec.frame)

    # Full-axes background = the frame border.
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.add_patch(Rectangle((0, 0), 1, 1, facecolor=frame_col, edgecolor="none"))
    if spec.frame == "LEGENDARY":
        # Double border + corner bursts for the chase tier.
        ax.add_patch(
            Rectangle(
                (0.028, 0.028), 0.944, 0.944, fill=False,
                edgecolor=_TEXT, lw=0.8, alpha=0.5,
            )
        )
        for cx, cy in ((0.05, 0.05), (0.95, 0.05), (0.05, 0.95), (0.95, 0.95)):
            ax.scatter([cx], [cy], marker="*", s=60, color=_TEXT, zorder=5)

    # Inner panel.
    ax.add_patch(
        FancyBboxPatch(
            (0.06, 0.06), 0.88, 0.88,
            boxstyle="round,pad=0.006,rounding_size=0.02",
            facecolor=_PANEL, edgecolor=_BG, lw=1.2,
        )
    )

    # Header: name + set label.
    name = spec.name
    fs = 13 if len(name) <= 22 else 11 if len(name) <= 30 else 9.5
    ax.text(
        0.5, 0.885, name, ha="center", va="top", fontsize=fs,
        color=_TEXT, fontweight="bold", wrap=True,
    )
    ax.text(
        0.5, 0.845, spec.set_label.upper() or spec.kind,
        ha="center", va="top", fontsize=6.5, color=_MUTED,
    )

    # Art window.
    art = ax.inset_axes([0.115, 0.30, 0.77, 0.50])
    art.set_xlim(0, 1)
    art.set_ylim(0, 1)
    art.axis("off")
    art.add_patch(
        FancyBboxPatch(
            (0, 0), 1, 1, boxstyle="round,pad=0.01,rounding_size=0.03",
            facecolor=_BG, edgecolor=frame_col, lw=0.8, alpha=1.0,
        )
    )
    _draw_motif(art, spec, rng)

    # Flavor strip.
    flavor = "\n".join(textwrap.wrap(spec.flavor, 34)[:3])
    if flavor:
        ax.text(
            0.5, 0.265, flavor, ha="center", va="top", fontsize=6.2,
            color=_MUTED, style="italic", linespacing=1.3,
        )

    # Stamp chips row (bottom, above the stat strip).
    chips = [s for s in spec.stamps if s in _STAMP_CHIP][:4]
    if chips:
        n = len(chips)
        w = min(0.16, 0.72 / n)
        total = n * w + (n - 1) * 0.015
        x0 = 0.5 - total / 2
        for i, s in enumerate(chips):
            x = x0 + i * (w + 0.015)
            ax.add_patch(
                FancyBboxPatch(
                    (x, 0.118), w, 0.030,
                    boxstyle="round,pad=0.004,rounding_size=0.008",
                    facecolor=frame_col, edgecolor="none", alpha=0.85,
                )
            )
            ax.text(
                x + w / 2, 0.133, _STAMP_CHIP[s], ha="center", va="center",
                fontsize=5.4, color=_BG, fontweight="bold",
            )

    # Stat strip: frame left, serial right.
    ax.text(
        0.115, 0.075, spec.frame, ha="left", va="center",
        fontsize=7.5, color=frame_col, fontweight="bold",
    )
    serial_txt = f"#{spec.serial}" if spec.serial is not None else ""
    if spec.first_edition:
        serial_txt = f"{serial_txt} · 1ST ED" if serial_txt else "1ST ED"
    ax.text(
        0.885, 0.075, serial_txt, ha="right", va="center",
        fontsize=7, color=_TEXT, family="DejaVu Sans Mono",
    )


def _render_face_png(spec: FaceSpec) -> io.BytesIO:
    fig = Figure(figsize=(_FACE_W, _FACE_H), facecolor=_BG)
    ax = fig.add_axes((0, 0, 1, 1))
    _draw_face(ax, spec)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=_DPI, facecolor=fig.get_facecolor())
    buf.seek(0)
    return buf


def _face_fp(spec: FaceSpec) -> tuple[Any, ...]:
    return (
        spec.card_key, spec.frame, spec.serial,
        tuple(spec.stamps), spec.first_edition,
    )


async def render_card_face(spec: FaceSpec) -> io.BytesIO:
    """Cached + threaded face render -- same pattern as render_candle_chart."""
    fp = _face_fp(spec)
    cached = _FACE_CACHE.get(fp)
    if cached is not None:
        _FACE_CACHE.move_to_end(fp)
        return io.BytesIO(cached)
    buf = await asyncio.to_thread(_render_face_png, spec)
    _FACE_CACHE[fp] = buf.getvalue()
    while len(_FACE_CACHE) > _FACE_CACHE_MAX:
        _FACE_CACHE.popitem(last=False)
    return buf


def _render_spread_png(specs: list[FaceSpec]) -> io.BytesIO:
    """All pulled cards side-by-side -- the pack summary screenshot."""
    n = len(specs)
    fig = Figure(figsize=(_FACE_W * n, _FACE_H), facecolor=_BG)
    for i, spec in enumerate(specs):
        ax = fig.add_axes((i / n + 0.012, 0.02, 1 / n - 0.024, 0.96))
        _draw_face(ax, spec)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=_DPI, facecolor=fig.get_facecolor())
    buf.seek(0)
    return buf


async def render_card_spread(specs: Sequence[FaceSpec]) -> io.BytesIO:
    specs = list(specs)
    fp: tuple[Any, ...] = ("spread", tuple(_face_fp(s) for s in specs))
    cached = _FACE_CACHE.get(fp)
    if cached is not None:
        _FACE_CACHE.move_to_end(fp)
        return io.BytesIO(cached)
    buf = await asyncio.to_thread(_render_spread_png, specs)
    _FACE_CACHE[fp] = buf.getvalue()
    while len(_FACE_CACHE) > _FACE_CACHE_MAX:
        _FACE_CACHE.popitem(last=False)
    return buf
