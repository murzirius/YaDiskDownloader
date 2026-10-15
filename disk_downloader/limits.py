MAX_WORKERS = 8


def normalize_saved_workers(settings):
    """Keep older histories/profiles usable after reducing the worker limit."""
    value = settings.get('workers')
    if isinstance(value, int) and not isinstance(value, bool) and value > MAX_WORKERS:
        settings['workers'] = MAX_WORKERS
        return True
    return False
