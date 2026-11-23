"""Deterministic prioritisation, shared by every check.

Same formula as the Eclipse Time engine, so a signal scored inside Odoo and
one scored by the Eclipse Time service mean the same thing:

    score = 55 * severity / 4            (severity 1..4)
          + 25 * min(occurrences, 20) / 20
          + 20 * confidence             (0..1, certainty of the rule)
          + round(weight * 100)         (bounded learning, -0.20..+0.20)
          + compound bonus              (15 per linked area, max 30)
          + trend bonus                 (15 when accelerating)

clamped to 0..100.  P0 >= 85, P1 >= 65 (or critical severity), P2 >= 40,
P3 otherwise.  Pure functions only: easy to audit and to unit test.
"""

WEIGHT_STEP = 0.05
WEIGHT_LIMIT = 0.20
COMPOUND_PER_AREA = 15
COMPOUND_MAX = 30
TREND_BONUS = 15


def clamp_weight(weight):
    return max(-WEIGHT_LIMIT, min(WEIGHT_LIMIT, round(weight or 0.0, 2)))


def is_accelerating(last_24h, previous_24h):
    """At least 3 events in the last 24 h and 50 % more than the 24 h before."""
    last_24h = last_24h or 0
    previous_24h = previous_24h or 0
    return last_24h >= 3 and last_24h > previous_24h * 1.5


def compound_bonus(linked_areas):
    """``linked_areas``: number of *other* areas with a related open signal."""
    if not linked_areas or linked_areas < 1:
        return 0
    return min(linked_areas * COMPOUND_PER_AREA, COMPOUND_MAX)


def compute_score(severity, occurrences, confidence, weight=0.0,
                  linked_areas=0, accelerating=False):
    severity = max(1, min(4, int(severity or 1)))
    occurrences = max(0, int(occurrences or 0))
    confidence = max(0.0, min(1.0, float(confidence or 0.0)))
    score = round(55.0 * severity / 4.0
                  + 25.0 * min(occurrences, 20) / 20.0
                  + 20.0 * confidence)
    score += round(clamp_weight(weight) * 100)
    score += compound_bonus(linked_areas)
    score += TREND_BONUS if accelerating else 0
    return max(0, min(100, int(score)))


def priority_from_score(score, severity):
    if score >= 85:
        return 'p0'
    if score >= 65 or int(severity or 0) >= 4:
        return 'p1'
    if score >= 40:
        return 'p2'
    return 'p3'
