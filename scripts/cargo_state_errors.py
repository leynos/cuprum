"""Define the error shared by Cargo state preparation and run-home helpers."""

from __future__ import annotations


class CargoStatePreparationError(RuntimeError):
    """Report a Cargo preparation failure without exposing cache credentials."""
