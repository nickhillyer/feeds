import json
from datetime import timedelta
from html import unescape
from urllib.parse import urlencode

from .common import RETENTION, fetch, instant, normalize, stamp, Tree

TITLE = 'Methow Valley News'
HOME = 'https://methowvalleynews.com'
SOURCE = HOME
SLUG = 'methow-valley-news'
API = HOME + '/wp-json/wp/v2/posts'


def article(post):
    authors = post.get('_embedded', {}).get('author', [])
    if not authors or any('name' not in author for author in authors):
        raise ValueError('Missing article author')
    return {
        'id': 'news:' + str(int(post['id'])),
        'title': normalize(Tree(unescape(post['title']['rendered'])).root.text()),
        'content_html': post['content']['rendered'],
        'authors': [{'name': author['name']} for author in authors],
        'url': post['link'],
    }


def collect(feed, now):
    state = feed['_feeds']
    cutoff = now - timedelta(days=7) if not state['initialized'] else max(now - RETENTION, instant(state['last_successful_at']) - timedelta(hours=3))
    posts = {}
    number = 1
    while True:
        data, headers = fetch(API + '?' + urlencode({'after': stamp(cutoff), 'per_page': 100, 'page': number, '_embed': 1, 'orderby': 'date', 'order': 'desc'}), with_headers=True)
        if 'json' not in headers.get('Content-Type', ''):
            raise ValueError('Expected WordPress JSON; possible access challenge')
        batch = json.loads(data)
        if not isinstance(batch, list):
            raise ValueError('Expected WordPress posts array')
        for post in batch:
            posts['news:' + str(int(post['id']))] = post
        if number >= int(headers['X-WP-TotalPages']):
            break
        number += 1
    return [article(post) for post in posts.values()]
