"""Summarise folded stack samples into JSON rankings."""

from __future__ import annotations

import argparse
import collections
import dataclasses as dc
import json
import pathlib as pth


def _parse_folded_line(line: str) -> tuple[tuple[str, ...], int] | None:
    """Parse one folded-stack line."""
    stripped = line.strip()
    if not stripped:
        return None
    stack_text, separator, count_text = stripped.rpartition(" ")
    if not separator:
        return None
    try:
        count = int(count_text)
    except ValueError:
        return None
    frames = tuple(frame for frame in stack_text.split(";") if frame)
    if not frames or count <= 0:
        return None
    return frames, count


def _percent(samples: int, total: int) -> float:
    """Return a rounded percentage for sample counts."""
    if total == 0:
        return 0.0
    return round(samples * 100.0 / total, 4)


@dc.dataclass(slots=True)
class _FoldedSummaryState:
    """Accumulated folded-stack counters."""

    inclusive: collections.Counter[str]
    leaf: collections.Counter[str]
    stacks: collections.Counter[str]
    examples: dict[str, list[str]]
    total: int = 0

    def add(self, frames: tuple[str, ...], samples: int, *, example_limit: int) -> None:
        """Add one parsed stack to the summary state."""
        stack_text = ";".join(frames)
        self.stacks[stack_text] += samples
        self.total += samples
        for frame in frames:
            self.inclusive[frame] += samples
            frame_examples = self.examples.setdefault(frame, [])
            if len(frame_examples) < example_limit:
                frame_examples.append(stack_text)
        self.leaf[frames[-1]] += samples


def _rank_frames(
    state: _FoldedSummaryState,
    ranking: collections.Counter[str],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Build ranked frame entries."""
    ranked = sorted(
        ranking,
        key=lambda frame: (-ranking[frame], -state.leaf[frame], frame),
    )
    return [
        {
            "frame": frame,
            "inclusive_samples": state.inclusive[frame],
            "inclusive_percent": _percent(state.inclusive[frame], state.total),
            "leaf_samples": state.leaf[frame],
            "leaf_percent": _percent(state.leaf[frame], state.total),
            "example_stacks": state.examples.get(frame, []),
        }
        for frame in ranked[:limit]
    ]


def summarize_folded_file(
    folded_path: pth.Path,
    *,
    output: pth.Path,
    limit: int = 30,
    example_limit: int = 3,
) -> dict[str, object]:
    """Summarise a folded stack file into an agent-friendly JSON document."""
    state = _FoldedSummaryState(
        inclusive=collections.Counter(),
        leaf=collections.Counter(),
        stacks=collections.Counter(),
        examples={},
    )

    for line in folded_path.read_text(errors="replace").splitlines():
        parsed = _parse_folded_line(line)
        if parsed is None:
            continue
        frames, samples = parsed
        state.add(frames, samples, example_limit=example_limit)

    top_inclusive = _rank_frames(state, state.inclusive, limit=limit)
    top_leaf = _rank_frames(state, state.leaf, limit=limit)
    top_stacks = [
        {
            "stack": stack,
            "samples": samples,
            "percent": _percent(samples, state.total),
        }
        for stack, samples in state.stacks.most_common(limit)
    ]
    summary: dict[str, object] = {
        "total_samples": state.total,
        "top_inclusive_frames": top_inclusive,
        "top_leaf_frames": top_leaf,
        "top_stacks": top_stacks,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def _parse_args() -> argparse.Namespace:
    """Parse summariser CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folded", type=pth.Path)
    parser.add_argument("--output", type=pth.Path, required=True)
    parser.add_argument("--limit", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    """Run the folded-stack summariser CLI."""
    args = _parse_args()
    summarize_folded_file(args.folded, output=args.output, limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
