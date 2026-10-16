import platform
import re
from urllib.parse import urlencode

from . import __version__, __build__
from .updates import repository


def draft(data, engine, repo):
    title, description = data.get("title", ""), data.get("description", "")
    if not isinstance(title, str) or not 3 <= len(title.strip()) <= 120 or not isinstance(description, str) or not 10 <= len(description.strip()) <= 2500:
        raise ValueError("Заголовок: 3–120 символов; описание: 10–2500 символов")
    token = engine.api.credentials.token
    def clean(value):
        if token:
            value = value.replace(token, "[ключ скрыт]")
        value = re.sub(r"\b(Bearer|OAuth)\s+\S+", r"\1 [ключ скрыт]", value, flags=re.I)
        return re.sub(r"\b(access_token|refresh_token|client_secret)\s*[=:]\s*[^\s&,;]+", r"\1=[ключ скрыт]", value, flags=re.I)
    title, description = clean(title.strip()), clean(description.strip())
    body = description + f"\n\n### Среда\nВерсия: {__version__}\nСборка: {__build__}\nОС: {platform.system()} {platform.release()}\nАрхитектура: {platform.machine()}\n"
    job_id = data.get("job_id")
    if job_id:
        status = engine.status_cache.snapshot(engine.store.job(job_id))
        keys = ('phase',) if status.get('statistics_pending') else ('phase', 'files', 'processed', 'errors', 'scan_errors')
        body += "\n### Состояние задания\n" + "\n".join(f"{key}: {status[key]}" for key in keys)
        if status.get('statistics_pending'):
            body += '\nСчётчики ещё рассчитываются в фоне.'
    url = "https://github.com/" + repository(repo) + "/issues/new?" + urlencode({"title": title, "body": body})
    if len(url) > 7500:
        raise ValueError("Сократите описание для открытия формы GitHub; подробности можно дописать в форме")
    return {"title": title, "body": body, "url": url}
