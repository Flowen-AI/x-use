"""Render a dated README chart from GitHub's public stargazer timestamps.

Maintainer utility (requires gh authentication and optional matplotlib):
    uv run --no-project --with matplotlib python scripts/update_star_history.py

Only aggregate counts are written. Tokens and stargazer identities are never
sent to a chart service or included in the asset. Unstar events are unavailable.
"""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess


REPO = "ihuzaifashoukat/x-use"
ROOT = Path(__file__).resolve().parents[1]


def github(endpoint, *options):
    result = subprocess.run(
        ["gh", "api", endpoint, *options],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def main():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    metadata = github(f"repos/{REPO}")
    pages = github(
        f"repos/{REPO}/stargazers?per_page=100", "--paginate", "--slurp",
        "-H", "Accept: application/vnd.github.star+json",
    )
    timestamps = [datetime.fromisoformat(item["starred_at"].replace("Z", "+00:00"))
                  for page in pages for item in page]
    if len(timestamps) != metadata["stargazers_count"]:
        raise SystemExit("Star count changed during retrieval; retry to obtain a consistent snapshot.")
    now = datetime.now(timezone.utc)
    start = datetime.fromisoformat(metadata["created_at"].replace("Z", "+00:00"))
    daily = Counter(stamp.date() for stamp in timestamps)
    dates, totals = [start], [0]
    for day, count in sorted(daily.items()):
        dates.append(datetime.combine(day, datetime.min.time(), timezone.utc))
        totals.append(totals[-1] + count)
    dates.append(now)
    totals.append(len(timestamps))

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11,
                         "svg.fonttype": "none", "svg.hashsalt": "x-use-star-history"})
    fig, ax = plt.subplots(figsize=(10, 4.8), dpi=160, facecolor="#ffffff")
    fig.subplots_adjust(left=.09, right=.95, top=.75, bottom=.24)
    fig.text(.09, .90, "x-use / Star history", size=20, weight="bold", color="#111827")
    fig.text(.09, .83, f"{len(timestamps):,} current stars  ·  Snapshot {now:%Y-%m-%d} UTC",
             size=12, color="#4b5563")
    ax.step(dates, totals, where="post", color="#2563eb", linewidth=2.4)
    ax.fill_between(dates, totals, step="post", alpha=.08, color="#2563eb")
    ax.scatter([now], [totals[-1]], s=35, color="#2563eb", zorder=3)
    ax.set_xlim(start, now)
    ax.set_ylim(0, max(5, totals[-1] * 1.12))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=5, integer=True))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=6))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.grid(axis="y", color="#e5e7eb", linewidth=.8)
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", colors="#4b5563", length=0, pad=10)
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.text(.09, .09, "Source: GitHub API · Cumulative stars from current stargazers; removed stars excluded.",
             size=9, color="#6b7280")
    target = ROOT / "assets" / "star-history.svg"
    fig.savefig(target, metadata={"Date": now.strftime("%Y-%m-%d"),
                                  "Description": "Dated cumulative star snapshot; removed stars excluded."})
    target.write_text("\n".join(line.rstrip() for line in target.read_text(encoding="utf-8").splitlines()) + "\n",
                      encoding="utf-8")
    # A local PNG is useful for visual review; only the SVG is published.
    preview = ROOT / ".cache" / "star-history-preview.png"
    preview.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(preview)
    plt.close(fig)
    print(f"Rendered {len(timestamps)} stars to {target.relative_to(ROOT)} ({now:%Y-%m-%d} UTC)")


if __name__ == "__main__":
    main()
