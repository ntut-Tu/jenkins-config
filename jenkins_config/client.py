"""Authenticated Jenkins API adapter with bounded waits and CSRF support."""
import base64
import http.client
import http.cookiejar
import json
import time
import urllib.error
import urllib.request


class JenkinsClient:
    def __init__(self, url, username, password, opener=None):
        self.url = url.rstrip('/') + '/'
        self.authorization = 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path, data=None, content_type=None):
        headers = {'Authorization': self.authorization}
        if data is not None:
            crumb = json.loads(self.request('crumbIssuer/api/json'))
            headers[crumb['crumbRequestField']] = crumb['crumb']
            if content_type:
                headers['Content-Type'] = content_type
        request = urllib.request.Request(self.url + path, data=data, headers=headers)
        with self.opener.open(request, timeout=15) as response:
            return response.read()

    def wait(self, path='api/json', timeout=300, predicate=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = self.request(path)
                if predicate is None or predicate(response):
                    return response
            except urllib.error.HTTPError as error:
                if error.code not in (404, 502, 503, 504):
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException):
                pass
            time.sleep(2)
        raise TimeoutError(f'Jenkins did not become ready within {timeout}s: {path}')
