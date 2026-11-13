"""Colour palette (dark minimal theme, cyan/blue accents)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Palette:
    """Rich style strings used across the UI."""

    accent: str = "bold cyan"
    accent_alt: str = "bold dodger_blue2"
    text: str = "grey85"
    dim: str = "grey50"
    ascii_art: str = "grey93"
    success: str = "green"
    warning: str = "yellow"
    error: str = "bold red"


PALETTE = Palette()
