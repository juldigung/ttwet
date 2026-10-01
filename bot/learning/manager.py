"""Platzhalter – wird in Schritt 9 vollständig umgesetzt."""

from __future__ import annotations


class LearningManager:
    def __init__(self, cfg):
        self.cfg = cfg

    def periodic(self, engine) -> None:
        return None

    def handle_command(self, engine, ctype: str, payload: dict):
        return False, "Das Lernsystem ist noch nicht verfügbar."
