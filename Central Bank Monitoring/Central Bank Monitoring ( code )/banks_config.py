# file: banks_config.py
from cb_sources import BOE, FED, ECB, BOJ, BOC, RBA, RBNZ, SNB

# Banques à surveiller
ACTIVE_BANKS = [
    BOE,
    FED,
    ECB,
    BOJ,
    BOC,
    RBA,
    RBNZ,
    SNB,
]

# Timers (peuvent être surchargés par banque)
BANK_TIMERS = {
    "default": {
        "poll_sec": 0.2,       # fréquence de check du RSS
        "cooldown_sec": 60,    # délai mini entre 2 analyses si même item
        "heartbeat_sec": 60,   # affichage "Heartbeat OK"
    },
    # Exemples de overrides:
    # "boe": {"poll_sec": 0.1, "cooldown_sec": 45, "heartbeat_sec": 30},
    # "fed": {"poll_sec": 0.2, "cooldown_sec": 60, "heartbeat_sec": 60},
}
