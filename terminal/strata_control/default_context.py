"""The conversation context a model starts with: 128K on every card, unless the person picks another (owner, 2026-11-01).

One rule for the app (app/host/src/lib/default-context.ts says the same), the Terminal (`strata launch`, `setup`) and the MCP tools: 128K, or the
largest size the model offers when that is under 128K. The VRAM tiers that used to lower it on small
cards are gone; a card that 128K leaves little room on gets a warning, never a lower number (the engine says plainly if it cannot start).
A Mac keeps its unified-memory rule (strata_control/mac_fit.py).

The smallest size a model is offered is MIN_CONTEXT (64K).  It is a rule of the catalog the pickers read, and the size a person picks is
handed to the engine as --max-context (strata_cli/launch/runner.py, setup.py): the engine has no floor of its own.
"""
from __future__ import annotations

from typing import Iterable

DEFAULT_CONTEXT = 131072

#: The smallest conversation memory a model is offered (owner, 2026-11-25): 65,536 tokens, the same low step LM Studio's own settings
#: give these models (its 64K).  8K and 32K used to be offered; at those sizes the agent's compaction, which is ordered from the window,
#: has almost no room left before the model runs out.  Every menu (setup.py's CONTEXTS, hy4_fit, inkling_fit, mac_fit, own_path_fit), the
#: `--context` check and the MCP schema take their floor from here; a test keeps them all at it.
MIN_CONTEXT = 65536


def pick(sizes: Iterable[int] | None = None) -> int:
    """128K, or the largest of `sizes` under it, or (when every size is longer) the smallest."""
    have = sorted(int(s) for s in (sizes or ()) if int(s) > 0)
    under = [s for s in have if s <= DEFAULT_CONTEXT]
    return under[-1] if under else (have[0] if have else DEFAULT_CONTEXT)


def offered(sizes: Iterable[int] | None = None) -> list[int]:
    """The sizes a picker offers: those at or above MIN_CONTEXT, and the floor itself when the list has nothing at or above it."""
    have = sorted({int(s) for s in (sizes or ()) if int(s) > 0})
    return [s for s in have if s >= MIN_CONTEXT] or [MIN_CONTEXT]
