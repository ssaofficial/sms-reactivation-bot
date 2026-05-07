"""
optimizer.py — Thompson Sampling Bandit
Selects the best message variant for each sequence step.
Learns from reply rates and shifts traffic toward winners automatically.
"""

import random
import logging
import time
from database import (
    init_bandit_variants, get_variants_for_step,
    record_variant_result, increment_variant_sent
)
from playbook_loader import get_sequence

logger = logging.getLogger(__name__)


def initialize_variants():
    """Seed all variants from playbook into the bandit DB.
    Deletes any DB variants whose step+id combo is NOT in the current playbook
    so stale variants from old playbook edits are never selected.
    """
    sequence = get_sequence()
    all_variants = []
    current_ids = set()

    for step_key, step_data in sequence.items():
        variants = step_data.get("variants", [])
        for v in variants:
            all_variants.append({
                "id": v["id"],
                "step_name": step_key,
                "variant_label": v["id"],
                "message_template": str(v["bubbles"])
            })
            current_ids.add(v["id"])

    # Purge stale variants: delete any DB row whose variant_id is not in current playbook
    if current_ids:
        from database import get_conn
        conn = get_conn()
        placeholders = ",".join("?" for _ in current_ids)
        deleted = conn.execute(
            f"DELETE FROM bandit_variants WHERE id NOT IN ({placeholders})",
            list(current_ids)
        ).rowcount
        conn.commit()
        conn.close()
        if deleted:
            logger.info(f"[OPTIMIZER] Purged {deleted} stale bandit variants not in current playbook")

    if all_variants:
        init_bandit_variants(all_variants)
        logger.info(f"[OPTIMIZER] Initialized {len(all_variants)} variants")


def select_variant(step_name: str) -> dict:
    """
    Thompson Sampling: sample from Beta distribution for each variant.
    Returns the variant dict with the highest sample.
    Falls back to first variant if no variants in DB.
    """
    variants = get_variants_for_step(step_name)

    if not variants:
        # Fallback: get from playbook directly
        from playbook_loader import get_step_variants
        playbook_variants = get_step_variants(step_name)
        if playbook_variants:
            return playbook_variants[0]
        return {"id": "fallback", "bubbles": [{"text": "hey", "delay_seconds": 0}]}

    # Thompson Sampling: draw from Beta(alpha, beta) for each variant
    best_variant = None
    best_sample = -1

    for v in variants:
        alpha = v.get("alpha", 1.0)
        beta = v.get("beta", 1.0)
        sample = random.betavariate(alpha, beta)

        if sample > best_sample:
            best_sample = sample
            best_variant = v

    # Parse bubbles from stored template string back to list
    import ast
    try:
        bubbles = ast.literal_eval(best_variant["message_template"])
    except Exception:
        from playbook_loader import get_step_variants
        playbook_variants = get_step_variants(step_name)
        for pv in playbook_variants:
            if pv["id"] == best_variant["id"]:
                bubbles = pv["bubbles"]
                break
        else:
            bubbles = [{"text": "hey", "delay_seconds": 0}]

    logger.info(f"[OPTIMIZER] Selected variant {best_variant['id']} for step {step_name} "
                f"(alpha={best_variant.get('alpha', 1):.2f}, beta={best_variant.get('beta', 1):.2f})")

    return {
        "id": best_variant["id"],
        "bubbles": bubbles
    }


def record_send(variant_id: str):
    """Record that a variant was sent."""
    increment_variant_sent(variant_id)


def record_result(variant_id: str, replied: bool, positive: bool = True):
    """
    Record the outcome of a variant send.
    replied: did the contact reply at all
    positive: was the reply positive/neutral (not opt-out or hostile)
    """
    if replied:
        record_variant_result(variant_id, positive=positive)
        logger.info(f"[OPTIMIZER] Recorded {'positive' if positive else 'negative'} result for {variant_id}")


def get_performance_report() -> str:
    """Generate a human-readable performance report for all variants."""
    sequence = get_sequence()
    lines = ["=== BANDIT PERFORMANCE REPORT ===\n"]

    for step_key in sequence.keys():
        variants = get_variants_for_step(step_key)
        if not variants:
            continue

        lines.append(f"\nStep: {step_key}")
        lines.append("-" * 40)

        for v in sorted(variants, key=lambda x: x.get("total_replied", 0) / max(x.get("total_sent", 1), 1), reverse=True):
            sent = v.get("total_sent", 0)
            replied = v.get("total_replied", 0)
            positive = v.get("total_positive", 0)
            reply_rate = (replied / sent * 100) if sent > 0 else 0
            positive_rate = (positive / replied * 100) if replied > 0 else 0

            lines.append(
                f"  {v['id']}: sent={sent} | replied={replied} ({reply_rate:.1f}%) | "
                f"positive={positive} ({positive_rate:.1f}%) | "
                f"alpha={v.get('alpha', 1):.2f} beta={v.get('beta', 1):.2f}"
            )

    return "\n".join(lines)


if __name__ == "__main__":
    from database import init_db
    init_db()
    initialize_variants()
    print("Variants initialized.")
    print("\nTest selection:")
    for step in ["step_0_opener", "step_1_reintro", "step_2_ai_curiosity", "step_4_soft_close"]:
        v = select_variant(step)
        print(f"  {step} -> {v['id']}")
    print("\n" + get_performance_report())
