"""Recover a verified publication interrupted between rename and DB commit."""
import os
from .paths import safe_join
from .service_outputs import digest_file


def recover_publications(store):
    recovered = 0
    for row in store.rows('SELECT * FROM pending_service_outputs'):
        try:
            target=safe_join(row['root'],row['local'])
            if os.path.isfile(target) and os.stat(target).st_nlink==1 and digest_file(target)==row['sha256']:
                with store.db() as db:
                    db.execute('INSERT OR REPLACE INTO service_outputs VALUES(?,?,?,?)',tuple(row))
                    db.execute('DELETE FROM pending_service_outputs WHERE root=? AND kind=?',(row['root'],row['kind']))
        except (OSError,ValueError):
            continue
    for row in store.rows('SELECT p.*,j.root FROM pending_publications p JOIN jobs j ON j.id=p.job'):
        try:
            target = safe_join(row['root'], row['local'])
            if not os.path.isfile(target) or os.stat(target).st_nlink != 1 or os.path.getsize(target) != row['size'] or digest_file(target) != row['sha256']:
                continue
            with store.db() as db:
                db.execute('INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?,?,?,?,?)', tuple(row[k] for k in ('job','path','type','local','size','sha256','fingerprint','preview_size')))
                db.execute('DELETE FROM partials WHERE job=? AND path=? AND type=?', (row['job'],row['path'],row['type']))
                db.execute('DELETE FROM partial_ownership WHERE job=? AND local=?', (row['job'],row['temp']))
                db.execute('DELETE FROM pending_publications WHERE job=? AND path=? AND type=?', (row['job'],row['path'],row['type']))
            recovered += 1
        except (OSError, ValueError):
            continue
    return recovered
