MAX_WORKERS = 20


def normalize_saved_workers(settings):
    """Keep saved histories and profiles within the supported worker limit."""
    value = settings.get('workers')
    if isinstance(value, int) and not isinstance(value, bool) and value > MAX_WORKERS:
        settings['workers'] = MAX_WORKERS
        return True
    return False
