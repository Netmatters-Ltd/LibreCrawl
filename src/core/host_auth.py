"""HTTP Basic auth limited to the site being crawled"""
from urllib.parse import urlparse

from requests.auth import AuthBase, HTTPBasicAuth


class HostBasicAuth(AuthBase):
    """Adds Basic auth only to requests for one host.

    A crawl also checks the external links it finds, so a session-wide
    Authorization header would hand the site's credentials to every third
    party it links to. requests itself drops the header when a redirect
    leaves the host.
    """

    def __init__(self, host, username, password):
        self.host = host.lower()
        self.basic = HTTPBasicAuth(username, password)

    def __call__(self, request):
        if (urlparse(request.url).netloc or '').lower() == self.host:
            return self.basic(request)
        return request


def parse_http_auth(value):
    """Validate a start request's httpAuth value into (username, password), or None"""
    if not value:
        return None
    if not isinstance(value, dict):
        raise ValueError('httpAuth must be an object with username and password')

    username = value.get('username')
    password = value.get('password')
    if not isinstance(username, str) or not isinstance(password, str) or not username:
        raise ValueError('httpAuth needs a username and a password')

    return username, password
