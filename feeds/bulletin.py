import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from .common import RETENTION, instant, normalize, page

TITLE = 'Methow Valley Bulletin Board'
HOME = 'https://methowvalley.com'
ICON = 'https://methowvalley.com/favicon.ico'
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


def collect(feed, now):
    state = feed['_feeds']
    cutoff = now - timedelta(days=1) if not state['initialized'] else max(now - RETENTION, instant(state['last_successful_at']) - timedelta(hours=3))
    known = {item['id'].split(':', 1)[1] for item in feed['items'] if item['id'].startswith('topic:')}
    result = {}
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
            author = row.find(lambda n: n.has('topic-poster'))
            if author is None:
                raise ValueError('Missing topic creation date')
            created = posted(normalize(author.text()))
            pinned = row.find(lambda n: 'sticky' in n.attrs.get('class', '') or 'announce' in n.attrs.get('class', '')) is not None
            if not pinned:
                dates.append(created)
            if created >= cutoff and topic_id not in known:
                title = normalize(link.text())
                result[topic_id] = {'id': 'topic:' + str(int(topic_id)), 'title': title, 'content_text': '', 'url': topic_url(topic_id)}
        next_page = root.find(lambda n: n.tag == 'a' and n.attrs.get('rel') == 'next')
        if not next_page or (dates and max(dates) < cutoff):
            break
        next_offset = int(parse_qs(urlsplit(next_page.attrs['href']).query)['start'][0])
        if next_offset <= offset:
            raise ValueError('Forum pagination did not advance')
        offset = next_offset
    return [result[topic_id] for topic_id in sorted(result, key=int, reverse=True)]
