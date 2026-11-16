import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

UTC = timezone.utc
RETENTION = timedelta(days=30)
_REQUEST_COUNTS = ContextVar('request_counts', default=None)
_REQUEST_SOURCE = ContextVar('request_source', default=None)


@contextmanager
def count_requests(counts):
    token = _REQUEST_COUNTS.set(counts)
    try:
        yield
    finally:
        _REQUEST_COUNTS.reset(token)


@contextmanager
def request_source(source):
    token = _REQUEST_SOURCE.set(source)
    try:
        yield
    finally:
        _REQUEST_SOURCE.reset(token)



class SourceChallenge(ValueError):
    pass


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def instant(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp must include a timezone')
    return result.astimezone(UTC)


def fetch(url, with_headers=False):
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'Feeds/1.0 (+https://github.com/nickhillyer/feeds)', 'Cache-Control': 'no-cache'})
            counts = _REQUEST_COUNTS.get()
            if counts is not None:
                source = _REQUEST_SOURCE.get() or 'other'
                counts[source] = counts.get(source, 0) + 1
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()
                if 'html' in response.headers.get('Content-Type', '') and b'/.well-known/sgcaptcha/' in data[:8192].lower():
                    raise SourceChallenge(f'SiteGround access challenge (HTTP {response.status}); source content was not returned')
                if with_headers:
                    return data, response.headers
                return data, response.headers.get('Content-Type', '')
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, SourceChallenge):
            if attempt == 2:
                raise
        time.sleep(2 ** (attempt + 1))


class Node:
    def __init__(self, tag='', attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def has(self, name):
        return name in self.attrs.get('class', '').split()

    def find(self, predicate):
        return next(self.walk(predicate), None)

    def walk(self, predicate):
        for child in self.children:
            if isinstance(child, Node):
                if predicate(child):
                    yield child
                yield from child.walk(predicate)

    def text(self):
        if self.tag in ('script', 'style'):
            return ''
        result = ''.join(child.text() if isinstance(child, Node) else child for child in self.children)
        return result + ('\n' if self.tag in ('p', 'div', 'br', 'li', 'h2', 'h3') else '')


class Tree(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in ('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'):
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def normalize(text):
    lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def required(node, predicate, label):
    found = node.find(predicate)
    if found is None:
        raise ValueError('Missing ' + label)
    return found


def page(url):
    data, content_type = fetch(url)
    if 'html' not in content_type:
        raise ValueError('Expected HTML')
    return Tree(data.decode('utf-8')).root


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


def validate(feed):
    if feed.get('version') != 'https://jsonfeed.org/version/1.1' or not isinstance(feed.get('items'), list):
        raise ValueError('Invalid JSON Feed history')
    if not isinstance(feed.get('title'), str) or not isinstance(feed.get('_feeds', {}).get('initialized'), bool):
        raise ValueError('Missing feed lifecycle metadata')
    metadata = feed['_feeds']
    if metadata['initialized']:
        instant(metadata['last_successful_at'])
    ids = set()
    for item in feed['items']:
        if not isinstance(item.get('id'), str) or item['id'] in ids:
            raise ValueError('Invalid or duplicate item ID')
        ids.add(item['id'])
        instant(item['date_published'])
        if not ('content_text' in item or 'content_html' in item):
            raise ValueError('Missing item content')


def merge(previous, incoming, now):
    items = {item['id']: dict(item) for item in previous if instant(item['date_published']) >= now - RETENTION}
    for candidate in incoming:
        item = dict(candidate)
        old = items.get(item['id'])
        if old:
            changed = {k: v for k, v in old.items() if k not in ('date_published', 'date_modified')} != item
            item['date_published'] = old['date_published']
            if changed:
                item['date_modified'] = stamp(now)
            elif 'date_modified' in old:
                item['date_modified'] = old['date_modified']
        else:
            item['date_published'] = stamp(now)
        items[item['id']] = item
    return sorted(items.values(), key=lambda item: (item['date_published'], item['id']), reverse=True)


def diagnostic(error):
    if isinstance(error, urllib.error.HTTPError):
        return f'HTTP {error.code}'
    if isinstance(error, (TimeoutError, urllib.error.URLError, ConnectionError)):
        return 'Network request failed or timed out'
    message = str(error)
    message = re.sub(r'https?://\S+', '[source URL]', message)
    message = re.sub(r'(?i)sid=[^\s&]+', 'sid=[redacted]', message)
    return f'{type(error).__name__}: {message[:400]}'
