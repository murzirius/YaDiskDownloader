import fnmatch
from .paths import remote_path


def validate(values):
    if not isinstance(values, list) or len(values) > 500:
        raise ValueError("Исключения: до 500 путей или шаблонов")
    result = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value) > 1024 or any(ord(c) < 32 for c in value):
            raise ValueError("Некорректное исключение")
        value = value.strip().replace("\\", "/")
        if value.startswith("disk:/"):
            value = remote_path(value)
        elif value.startswith("/") or ".." in value.split("/") or ":" in value:
            raise ValueError("Исключение: путь disk:/… или шаблон, например *.tmp")
        if value not in result:
            result.append(value)
    return result


def excluded(path, settings):
    relative = path[6:]
    parts = relative.split("/")
    ancestors = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    for rule in settings.get("exclusions", []):
        if rule.startswith("disk:/"):
            if rule == "disk:/" or path == rule or path.startswith(rule.rstrip("/") + "/"):
                return True
        else:
            rule = rule.casefold().rstrip("/")
            for ancestor in ancestors:
                candidate = ancestor if "/" in rule else ancestor.rsplit("/", 1)[-1]
                if fnmatch.fnmatchcase(candidate.casefold(), rule):
                    return True
    return False
