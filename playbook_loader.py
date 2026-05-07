"""
playbook_loader.py — Loads and provides access to playbook.json
"""

import json
import os

_PLAYBOOK = None

def load_playbook():
    global _PLAYBOOK
    if _PLAYBOOK is None:
        path = os.path.join(os.path.dirname(__file__), "playbook.json")
        with open(path, "r") as f:
            _PLAYBOOK = json.load(f)
    return _PLAYBOOK


def get_persona():
    return load_playbook()["persona"]


def get_sequence():
    return load_playbook()["sequence"]


def get_objection_handlers():
    return load_playbook()["objection_handlers"]


def get_opt_out_keywords():
    return load_playbook().get("opt_out_keywords", [])


def get_score_events():
    return load_playbook().get("score_events", {})


def get_step_variants(step_key: str) -> list:
    """Return list of variant dicts for a given sequence step key."""
    seq = get_sequence()
    step = seq.get(step_key, {})
    return step.get("variants", [])


def get_followup_bubbles(followup_key: str) -> list:
    """Return bubbles for a follow-up step (no variants)."""
    seq = get_sequence()
    step = seq.get(followup_key, {})
    return step.get("bubbles", [])


def get_objection_response(intent: str) -> dict:
    """Return the objection handler dict for a given intent label."""
    handlers = get_objection_handlers()
    return handlers.get(intent, {})


def get_tone_rules() -> list:
    return get_persona().get("tone_rules", [])


def get_humor_library() -> list:
    return get_persona().get("humor_library", [])
