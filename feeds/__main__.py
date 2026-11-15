import argparse
import json
import sys
import urllib.error
from datetime import datetime
from pathlib import Path

from . import bulletin, news, trails
from .common import UTC, RETENTION, diagnostic, fetch, fingerprint, instant, merge, stamp, validate

ADAPTERS = (news, trails, bulletin)


class Status:
    TITLE = 'Feeds Update Status'
    SOURCE = 'https://github.com/nickhillyer/feeds'
    SLUG = 'status'
    ICON = 'https://github.githubassets.com/favicons/favicon.svg'


def empty_feed(adapter, url):
    return {'version': 'https://jsonfeed.org/version/1.1', 'title': adapter.TITLE, 'home_page_url': adapter.SOURCE, 'feed_url': url, 'icon': adapter.ICON, 'items': [], '_feeds': {'initialized': False}}


def load(adapter, base, bootstrap, reset=False):
    url = base.rstrip('/') + '/' + adapter.SLUG + '.json'
    if reset:
        return empty_feed(adapter, url)
    try:
        data, _ = fetch(url)
    except urllib.error.HTTPError as error:
        if error.code != 404 or not bootstrap:
            raise
        return empty_feed(adapter, url)
    feed = json.loads(data)
    validate(feed)
    if feed.get('feed_url') != url:
        raise ValueError('History feed URL does not match expected URL')
    return feed


def load_status(base, reset=False):
    url = base.rstrip('/') + '/status.json'
    if reset:
        return empty_feed(Status, url)
    try:
        data, _ = fetch(url)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return empty_feed(Status, url)
    feed = json.loads(data)
    # The old status.json was a snapshot, not a JSON Feed.
    if isinstance(feed, dict) and set(feed) == {'source_errors', 'failures', 'feed_items'}:
        return empty_feed(Status, url)
    validate(feed)
    if feed.get('feed_url') != url:
        raise ValueError('Status history feed URL does not match expected URL')
    return feed


def status_entry(feeds, failures, now):
    counts = {slug: sum(not item['id'].startswith('error:') for item in feed['items']) for slug, feed in feeds}
    summary = {'source_errors': len(failures), 'failures': failures, 'feed_items': counts}
    return {'id': 'status:' + stamp(now), 'title': 'Feed update: ' + ('OK' if not failures else f'{len(failures)} source error(s)'), 'content_text': json.dumps(summary, ensure_ascii=False, indent=2)}


def generate_status(history, feeds, failures, now):
    history['items'] = merge(history['items'], [status_entry(feeds, failures, now)], now)
    history['_feeds'] = {'initialized': True, 'last_successful_at': stamp(now)}
    validate(history)
    return history


def error_item(adapter, operation, error, now):
    details = diagnostic(error)
    return {'id': 'error:' + fingerprint(stamp(now) + operation + details), 'title': adapter.TITLE + ': ' + operation + ' failed', 'content_text': f'Time: {stamp(now)}\nOperation: {operation}\nSource: {adapter.SOURCE}\nDetails: {details}', 'url': adapter.SOURCE}


def generate(adapter, feed, now):
    feed['items'] = [item for item in feed['items'] if instant(item['date_published']) >= now - RETENTION]
    incoming = []
    failures = []
    feed['icon'] = adapter.ICON
    try:
        incoming.extend(adapter.collect(feed, now))
        feed['_feeds'] = {'initialized': True, 'last_successful_at': stamp(now)}
    except Exception as error:
        failures.append('source processing: ' + diagnostic(error))
        incoming.append(error_item(adapter, 'source processing', error, now))
    feed['items'] = merge(feed['items'], incoming, now)
    validate(feed)
    return feed, failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='https://nickhillyer.github.io/feeds')
    parser.add_argument('--output', type=Path, default=Path('output'))
    parser.add_argument('--bootstrap', action='store_true')
    parser.add_argument('--reset', action='store_true')
    args = parser.parse_args()
    now = datetime.now(UTC)
    try:
        histories = [(adapter, load(adapter, args.base_url, args.bootstrap, args.reset)) for adapter in ADAPTERS]
        status_history = load_status(args.base_url, args.reset)
    except Exception as error:
        print('History recovery failed; deployment blocked: ' + diagnostic(error), file=sys.stderr)
        return 1
    feeds = []
    failures = []
    for adapter, history in histories:
        feed, errors = generate(adapter, history, now)
        feeds.append((adapter.SLUG, feed))
        failures.extend(adapter.TITLE + ': ' + error for error in errors)
    feeds.append((Status.SLUG, generate_status(status_history, feeds, failures, now)))
    args.output.mkdir(parents=True, exist_ok=True)
    for slug, feed in feeds:
        target = args.output / (slug + '.json')
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(feed, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(target)
    (args.output / '.nojekyll').touch()
    for failure in failures:
        print(failure, file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
