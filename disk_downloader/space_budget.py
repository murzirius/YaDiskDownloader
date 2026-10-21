"""Non-optional free-space floor; a small, bounded transfer may drain first."""
HARD_FREE_BYTES = 500 * 1024 * 1024
RESUME_FREE_BYTES = 1024 * 1024 * 1024
EMERGENCY_BYTES = 32 * 1024 * 1024
FINISH_BYTES = 64 * 1024 * 1024


def may_finish(transfers, free):
    remaining = tuple(transfers.values())
    return bool(remaining) and all(isinstance(value, int) and value >= 0 for value in remaining) and sum(remaining) <= min(FINISH_BYTES, max(0, free - EMERGENCY_BYTES))
