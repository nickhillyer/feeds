import argparse
import json
import sys
import urllib.error
from datetime import datetime
from pathlib import Path

from . import bulletin, news, trails
from .common import UTC, RETENTION, diagnostic, discover_icon, fetch, fingerprint, instant, merge, stamp, validate

ADAPTERS = (news, trails, bulletin)


def load(adapter, base, bootstrap):
    url = base.rstrip('/') + '/' + adapter.SLUG + '.json'
    try:
        data, _ = fetch(url)
    except urllib.error.HTTPError as error:
        if error.code != 404 or not bootstrap:
            raise
        return {'version': 'https://jsonfeed.org/version/1.1', 'title': adapter.TITLE, 'home_page_url': adapter.SOURCE, 'feed_url': url, 'items': [], '_feeds': {'initialized': False}}
    feed = json.loads(data)
    validate(feed)
    if feed.get('feed_url') != url:
        raise ValueError('History feed URL does not match expected URL')
    return feed


def error_item(adapter, operation, error, now):
    details = diagnostic(error)
    return {'id': 'error:' + fingerprint(stamp(now) + operation + details), 'title': adapter.TITLE + ': ' + operation + ' failed', 'content_text': f'Time: {stamp(now)}\nOperation: {operation}\nSource: {adapter.SOURCE}\nDetails: {details}', 'url': adapter.SOURCE}


def generate(adapter, feed, now):
    feed['items'] = [item for item in feed['items'] if instant(item['date_published']) >= now - RETENTION]
    incoming = []
    failures = []
    for operation, action in [('source processing', lambda: adapter.collect(feed, now)), ('icon discovery', lambda: discover_icon(adapter.HOME))]:
        try:
            result = action()
            if operation == 'source processing':
                incoming.extend(result)
                feed['_feeds'] = {'initialized': True, 'last_successful_at': stamp(now)}
            else:
                feed['icon'] = result
        except Exception as error:
            failures.append(operation + ': ' + diagnostic(error))
            incoming.append(error_item(adapter, operation, error, now))
    feed['items'] = merge(feed['items'], incoming, now)
    validate(feed)
    return feed, failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='https://nickhillyer.github.io/feeds')
    parser.add_argument('--output', type=Path, default=Path('output'))
    parser.add_argument('--bootstrap', action='store_true')
    args = parser.parse_args()
    now = datetime.now(UTC)
    try:
        histories = [(adapter, load(adapter, args.base_url, args.bootstrap)) for adapter in ADAPTERS]
    except Exception as error:
        print('History recovery failed; deployment blocked: ' + diagnostic(error), file=sys.stderr)
        return 1
    feeds = []
    failures = []
    for adapter, history in histories:
        feed, errors = generate(adapter, history, now)
        feeds.append((adapter.SLUG, feed))
        failures.extend(adapter.TITLE + ': ' + error for error in errors)
    args.output.mkdir(parents=True, exist_ok=True)
    for slug, feed in feeds:
        target = args.output / (slug + '.json')
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(feed, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(target)
    (args.output / '.nojekyll').touch()
    for failure in failures:
        print(failure, file=sys.stderr)
    (args.output / 'status.json').write_text(json.dumps({'source_errors': len(failures), 'failures': failures}), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
