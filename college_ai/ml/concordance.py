"""ACT-to-SAT score concordance table.

Based on the official College Board / ACT concordance tables.
Maps ACT composite scores to SAT total (Evidence-Based Reading + Math, 400-1600 scale).
"""

# ACT composite -> SAT total (400-1600)
# Source: https://collegereadiness.collegeboard.org/pdf/guide-2018-act-sat-concordance.pdf
ACT_TO_SAT = {
    36: 1590,
    35: 1540,
    34: 1500,
    33: 1460,
    32: 1430,
    31: 1400,
    30: 1370,
    29: 1340,
    28: 1310,
    27: 1280,
    26: 1240,
    25: 1210,
    24: 1180,
    23: 1140,
    22: 1110,
    21: 1080,
    20: 1040,
    19: 1010,
    18: 970,
    17: 930,
    16: 890,
    15: 850,
    14: 800,
    13: 760,
    12: 710,
    11: 670,
    10: 630,
    9: 590,
}

# Build reverse lookup: SAT -> ACT (approximate, using nearest)
SAT_TO_ACT = {}
_sorted_entries = sorted(ACT_TO_SAT.items(), key=lambda x: x[1])
for i, (act, sat) in enumerate(_sorted_entries):
    # Assign all SAT values in this range to this ACT score
    lower = sat
    upper = _sorted_entries[i + 1][1] if i + 1 < len(_sorted_entries) else 1600
    for s in range(lower, upper):
        SAT_TO_ACT[s] = act
SAT_TO_ACT[1600] = 36


def act_to_sat(act_score: float) -> int:
    """Convert an ACT composite score to SAT equivalent.

    Rounds to nearest integer ACT score for lookup.
    """
    rounded = max(9, min(36, round(act_score)))
    return ACT_TO_SAT.get(rounded, 1010)  # default to ~average


def sat_to_act(sat_score: float) -> int:
    """Convert a SAT total score to ACT equivalent.

    Rounds to nearest 10 for lookup.
    """
    rounded = max(590, min(1600, round(sat_score / 10) * 10))
    return SAT_TO_ACT.get(rounded, 21)  # default to ~average
