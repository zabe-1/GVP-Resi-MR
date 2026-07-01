"""Validation map: address point, radius circles, selected block-group
centroids.  Pure matplotlib (no geopandas dependency); good enough to eyeball
that the radius selection is sane."""

from __future__ import annotations

import math

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .geo import EARTH_RADIUS_MILES
from .pipeline import AddressResult

RADIUS_COLORS = ["#1b9e77", "#d95f02", "#7570b3", "#e7298a"]


def _circle_points(lat: float, lon: float, radius_miles: float, n: int = 240):
    ang = radius_miles / EARTH_RADIUS_MILES
    xs, ys = [], []
    for i in range(n + 1):
        theta = 2 * math.pi * i / n
        dlat = math.degrees(ang) * math.cos(theta)
        dlon = math.degrees(ang) * math.sin(theta) / math.cos(math.radians(lat))
        ys.append(lat + dlat)
        xs.append(lon + dlon)
    return xs, ys


def plot_address(result: AddressResult, radii: tuple[float, ...], out_path: str) -> None:
    radii = tuple(sorted(radii))
    fig, ax = plt.subplots(figsize=(9, 9))
    bgs = result.block_groups_current
    # innermost radius each block group belongs to determines its color
    for i, r in enumerate(radii):
        inner = radii[i - 1] if i > 0 else 0.0
        pts = [bg for bg in bgs if inner < bg.distance_miles <= r]
        if pts:
            ax.scatter([b.lon for b in pts], [b.lat for b in pts], s=6,
                       color=RADIUS_COLORS[i % len(RADIUS_COLORS)], alpha=0.6,
                       label=f"block groups ≤ {r:g} mi (n={sum(1 for b in bgs if b.distance_miles <= r)})")
    for i, r in enumerate(radii):
        xs, ys = _circle_points(result.lat, result.lon, r)
        ax.plot(xs, ys, color=RADIUS_COLORS[i % len(RADIUS_COLORS)], lw=1.5)
    ax.plot(result.lon, result.lat, marker="*", color="black", markersize=16,
            label="address")
    ax.set_title(f"{result.matched_address or result.address}\n"
                 f"block-group centroids within {', '.join(f'{r:g}' for r in radii)} mile radii "
                 f"(ACS {result.acs_year} geography)")
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_aspect(1.0 / math.cos(math.radians(result.lat)))
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
