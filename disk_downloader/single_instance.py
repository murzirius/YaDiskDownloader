"""Restore an existing local instance through the usual CSRF protected API."""
import hashlib
import os


def state_key(folder):
    return hashlib.sha256(os.path.normcase(os.path.realpath(folder)).encode('utf8')).hexdigest()


def activate_existing(folder, port):
    import http.cookiejar
    import json
    import re
    import urllib.request
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):return None
    base = f'http://127.0.0.1:{port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())
    try:
        with opener.open(base+'/', timeout=1) as response:
            page = response.read(2*1024*1024).decode('utf8')
        token = re.search(r'name="csrf-token" content="([A-Za-z0-9_-]{20,100})"', page)
        if not token:return False
        headers = {'Origin':base, 'X-CSRF-Token':token.group(1)}
        with opener.open(urllib.request.Request(base+'/api/runtime',headers=headers), timeout=1) as response:
            runtime = json.loads(response.read(16384))
        if runtime.get('application') != 'YaDiskDownloader' or runtime.get('state_key') != state_key(folder):return False
        with opener.open(urllib.request.Request(base+'/api/activate',data=b'{}',headers=headers|{'Content-Type':'application/json'}), timeout=1) as response:
            return json.loads(response.read(1024)).get('ok') is True
    except (OSError, ValueError, UnicodeError):
        return False
