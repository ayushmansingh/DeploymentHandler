"""Chart geometry for the Stats tab, worked out in Python.

Everything is inline SVG built here rather than a charting library, for one
hard reason: this server has no internet access, so a script tag pointing at a
CDN would render a blank rectangle. Paths computed server-side need nothing to
load and work with JavaScript turned off.

The specs below are not arbitrary - thin marks, a 4px rounded data-end square
at the baseline, a 2px gap of surface between neighbouring bars, hairline
gridlines one step off the surface, and one hue for the whole series. A single
measure is being shown (how often an app was opened), so there is one colour:
shading bars by their own length would spend the only free channel restating
the length the bar already shows.
"""
from __future__ import annotations

from datetime import date, timedelta

# A bar is capped rather than filling its slot, so the leftover is air.
BAR_THICKNESS = 18
BAR_GAP = 8          # >= the 2px surface gap, with room to breathe
BAR_RADIUS = 4       # the data-end only; the baseline end stays square


def bar_rows(items: list[tuple[str, int]]) -> list[dict]:
    """Horizontal bars, longest first, all one colour.

    Widths come back as percentages of the longest bar, so the track can be
    whatever width the card happens to be and the bars still share a baseline
    and a scale. The label and the value are drawn as HTML beside the bar
    rather than inside it: an app called "travel-conversation-intelligence"
    never fits in its own bar, and a clipped label is worse than one outside.
    """
    if not items:
        return []
    top = max(value for _, value in items) or 1
    return [
        {
            "name": name,
            "value": value,
            # Floored at a sliver so a real but tiny count stays a visible
            # mark rather than vanishing into the baseline.
            "percent": max(1.5, round(value / top * 100, 1)) if value else 0.0,
        }
        for name, value in items
    ]


def area_path(values: list[float], width: float = 640.0, height: float = 120.0,
              pad: float = 3.0) -> dict:
    """A single series over time, as a line plus a wash underneath it.

    Returns both paths and the baseline so the caller can draw the fill at a
    low opacity and the line at full strength, which is what keeps a large
    area from reading as a saturated block.
    """
    if not values:
        return {}
    top = max(values) or 1
    inner = height - pad * 2
    step = width / max(len(values) - 1, 1)

    points = [
        (round(i * step, 2),
         round(pad + inner - (v / top) * inner, 2))
        for i, v in enumerate(values)
    ]
    line = "M" + " L".join(f"{x},{y}" for x, y in points)
    area = (f"{line} L{points[-1][0]},{height} L{points[0][0]},{height} Z")
    return {
        "line": line,
        "area": area,
        "width": width,
        "height": height,
        "last_x": points[-1][0],
        "last_y": points[-1][1],
        "top": top,
    }


def day_range(days: int, today: date | None = None) -> list[str]:
    """Every day in the window, oldest first.

    The chart needs a point for a day nobody opened anything, and the database
    has no row for such a day - so the days come from the calendar and the
    counts are looked up against them.
    """
    end = today or date.today()
    return [(end - timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(days - 1, -1, -1)]


def series_for(days: list[str], counts: dict[str, int]) -> list[int]:
    return [counts.get(day, 0) for day in days]


def bucket_by_week(days: list[str], counts: dict[str, int]) -> list[dict]:
    """Weekly totals, for a window too long to read a day at a time.

    Ninety daily marks in 640 pixels is seven pixels each; the eye reads the
    noise rather than the trend. Weeks are what the question - is this growing
    - is actually asked in.
    """
    weeks: list[dict] = []
    for index in range(0, len(days), 7):
        chunk = days[index:index + 7]
        weeks.append({
            "start": chunk[0],
            "end": chunk[-1],
            "total": sum(counts.get(day, 0) for day in chunk),
        })
    return weeks
