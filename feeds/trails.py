from .common import fingerprint, normalize, page, required, stamp

TITLE = 'Methow Trails'
HOME = 'https://methowtrails.org'
SOURCE = HOME + '/conditions'
SLUG = 'methow-trails'


def extract(root):
    section = required(root, lambda n: n.attrs.get('id') == 'trail-updates-1', 'trail updates section')
    content = required(section, lambda n: n.has('prose'), 'trail update text')
    timestamp = required(section, lambda n: n.tag == 'time', 'trail update timestamp')
    text = normalize(content.text())
    if not text or not timestamp.attrs.get('datetime'):
        raise ValueError('Empty trail update or timestamp')
    return text, normalize(timestamp.text())


def collect(feed, now):
    text, shown_time = extract(page(SOURCE))
    previous = next((item for item in feed['items'] if item['id'].startswith('trails:')), None)
    if previous and previous['content_text'] == text:
        return []
    return [{'id': 'trails:' + fingerprint(stamp(now) + '\n' + text), 'title': 'Trail Update: ' + shown_time, 'content_text': text, 'url': SOURCE}]
