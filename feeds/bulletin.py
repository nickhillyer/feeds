import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from .common import RETENTION, instant, normalize, page, required

TITLE = 'Methow Valley Bulletin Board'
HOME = 'https://methowvalley.com'
SOURCE = HOME + '/bb/viewforum.php?f=3'
SLUG = 'methow-valley-bulletin-board'
DATE = r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]{2} \d{1,2}, \d{4} \d{1,2}:\d{2} [ap]m'


def posted(text):
    match = re.search(DATE, text)
    if not match:
        raise ValueError('Missing original post date')
    return datetime.strptime(match.group(), '%a %b %d, %Y %I:%M %p').replace(tzinfo=ZoneInfo('America/Los_Angeles'))


def topic_url(topic_id):
    return HOME + '/bb/viewtopic.php?t=' + str(int(topic_id))


def extract(root, topic_id):
    body = required(root, lambda n: n.has('postbody'), 'original post')
    title = required(body, lambda n: n.tag == 'h3', 'topic title')
    author_line = required(body, lambda n: n.has('author'), 'original post author line')
    author = required(author_line, lambda n: n.has('username') or n.has('username-coloured'), 'topic creator')
    content = required(body, lambda n: n.has('content'), 'original post content')
    created = posted(normalize(author_line.text()))
    return {'id': 'topic:' + str(int(topic_id)), 'title': normalize(title.text()), 'authors': [{'name': normalize(author.text())}], 'content_text': normalize(content.text()), 'url': topic_url(topic_id)}, created


def collect(feed, now):
    state = feed['_feeds']
    cutoff = now - timedelta(days=1) if not state['initialized'] else max(now - RETENTION, instant(state['last_successful_at']) - timedelta(hours=3))
    ids = {item['id'].split(':')[1] for item in feed['items'] if item['id'].startswith('topic:')}
    offset = 0
    while True:
        root = page(SOURCE + '&' + urlencode({'sk': 't', 'sd': 'd', 'start': offset}))
        rows = list(root.walk(lambda n: n.tag == 'li' and n.has('row')))
        if not rows:
            raise ValueError('Missing forum topic rows')
        dates = []
        for row in rows:
            link = row.find(lambda n: n.has('topictitle'))
            if link is None:
                continue
            topic_id = parse_qs(urlsplit(urljoin(SOURCE, link.attrs['href'])).query)['t'][0]
            author = required(row, lambda n: n.has('topic-poster'), 'topic creation date')
            created = posted(normalize(author.text()))
            pinned = row.find(lambda n: 'sticky' in n.attrs.get('class', '') or 'announce' in n.attrs.get('class', '')) is not None
            if not pinned:
                dates.append(created)
            if created >= cutoff:
                ids.add(topic_id)
        next_page = root.find(lambda n: n.tag == 'a' and n.attrs.get('rel') == 'next')
        if not next_page or (dates and max(dates) < cutoff):
            break
        next_offset = int(parse_qs(urlsplit(next_page.attrs['href']).query)['start'][0])
        if next_offset <= offset:
            raise ValueError('Forum pagination did not advance')
        offset = next_offset
    retained = {item['id'] for item in feed['items']}
    result = []
    for topic_id in sorted(ids, key=int, reverse=True):
        item, created = extract(page(topic_url(topic_id)), topic_id)
        if item['id'] in retained or created >= cutoff:
            result.append(item)
    return result
