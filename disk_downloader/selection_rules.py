"""Date and folder priorities shared by scanning and comparison."""
from datetime import date, datetime
from .paths import remote_path


def validate(data):
    result = {}
    for key in ('date_from','date_to'):
        value = data.get(key,'')
        if not isinstance(value,str) or len(value)>10:
            raise ValueError('Дата должна быть в формате ГГГГ-ММ-ДД')
        if value:
            try:date.fromisoformat(value)
            except ValueError:raise ValueError('Укажите существующую дату ГГГГ-ММ-ДД') from None
        result[key] = value
    if result['date_from'] and result['date_to'] and result['date_from']>result['date_to']:
        raise ValueError('Начало периода должно быть раньше его окончания')
    result['date_field'] = data.get('date_field','modified')
    if result['date_field'] not in ('modified','created','taken'):
        raise ValueError('Выберите дату изменения, создания или съёмки')
    result['include_unknown_dates'] = data.get('include_unknown_dates',True)
    if not isinstance(result['include_unknown_dates'],bool):raise ValueError('Укажите, включать ли файлы без даты')
    paths = data.get('priority_paths',[])
    if not isinstance(paths,list) or len(paths)>100:raise ValueError('Можно выбрать до 100 приоритетных папок')
    result['priority_paths'] = list(dict.fromkeys(remote_path(path) for path in paths))
    return result


def taken(meta):
    exif = meta.get('exif')
    return meta.get('taken') or (exif.get('date_time') if isinstance(exif,dict) else None)


def included(meta, settings):
    if not (settings.get('date_from') or settings.get('date_to')):return True
    field = settings.get('date_field','modified')
    value = taken(meta) if field=='taken' else meta.get(field)
    try:
        value = datetime.fromisoformat(value.replace('Z','+00:00')).date().isoformat()
    except (AttributeError,TypeError,ValueError):
        return settings.get('include_unknown_dates',True)
    return (not settings.get('date_from') or value>=settings['date_from']) and (not settings.get('date_to') or value<=settings['date_to'])
