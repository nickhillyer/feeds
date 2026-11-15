import contextlib
import io
import json
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import unittest
import urllib.error
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from feeds import bulletin, news, trails
from feeds.__main__ import ADAPTERS, generate, generate_status, load, load_status, main, status_entry
from feeds.common import UTC, SourceChallenge, Tree, diagnostic, fetch, merge, stamp, validate

NOW = datetime(2026, 11, 14, 23, tzinfo=UTC)


def history(items=(), initialized=True):
    state = {'initialized': initialized}
    if initialized:
        state['last_successful_at'] = stamp(NOW - timedelta(hours=3))
    return {'version': 'https://jsonfeed.org/version/1.1', 'title': 'Test', 'items': list(items), '_feeds': state}


def item(identifier='news:1', text='Original', when=NOW):
    return {'id': identifier, 'content_text': text, 'date_published': stamp(when)}


def trail_page(text='Snow', date='November 14, 2026, 2:00pm', weather='Cold'):
    return Tree(f'<div id="trail-updates-1"><section><div><time datetime="2026-11-14T14:00:00-08:00">{date}</time><div class="prose"><p>{text}</p></div></div><aside>{weather}</aside></section></div>').root


def topic_page(text='Original', reply='Reply'):
    return Tree(f'<div class="postbody"><h3>Topic</h3><p class="author">by <a class="username">Creator</a> » Sat Nov 14, 2026 2:42 pm</p><div class="content">{text}<br>Second line</div><div class="signature">Signature</div></div><div class="postbody"><h3>Re: Topic</h3><p>{reply}</p></div>').root


def forum_page(topic_id, date, next_offset=None):
    next_link = f'<a rel="next" href="./viewforum.php?f=3&amp;start={next_offset}">Next</a>' if next_offset else ''
    return Tree(f'<li class="row"><dl><dt><a class="topictitle" href="./viewtopic.php?t={topic_id}&amp;sid=secret">Topic</a><div class="topic-poster">by Creator » {date}</div></dt></dl></li>{next_link}').root


class RequestTests(unittest.TestCase):
    def response(self, body=b'[]', kind='application/json', status=200):
        response = MagicMock()
        response.read.return_value = body
        response.headers = {'Content-Type': kind}
        response.status = status
        response.__enter__.return_value = response
        return response

    def challenge(self):
        return self.response(b'<meta http-equiv="refresh" content="0;/.well-known/sgcaptcha/?r=secret">', 'text/html', 202)

    def test_challenge_then_recovery(self):
        with patch('feeds.common.urllib.request.urlopen', side_effect=[self.challenge(), self.response()]) as request, patch('feeds.common.time.sleep') as sleep:
            self.assertEqual(fetch('https://example.org'), (b'[]', 'application/json'))
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_challenge_exhaustion_is_explicit(self):
        with patch('feeds.common.urllib.request.urlopen', side_effect=[self.challenge() for _ in range(3)]) as request, patch('feeds.common.time.sleep') as sleep:
            with self.assertRaisesRegex(SourceChallenge, 'SiteGround access challenge.*202'):
                fetch('https://example.org')
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_disconnect_then_recovery(self):
        with patch('feeds.common.urllib.request.urlopen', side_effect=[ConnectionResetError(), self.response()]), patch('feeds.common.time.sleep'):
            self.assertEqual(fetch('https://example.org', with_headers=True), (b'[]', {'Content-Type': 'application/json'}))

    def test_http_retry_and_terminal_not_found(self):
        unavailable = urllib.error.HTTPError('https://example.org', 503, 'Unavailable', {}, None)
        with patch('feeds.common.urllib.request.urlopen', side_effect=[unavailable, self.response()]), patch('feeds.common.time.sleep'):
            self.assertEqual(fetch('https://example.org')[0], b'[]')
        missing = urllib.error.HTTPError('https://example.org', 404, 'Missing', {}, None)
        with patch('feeds.common.urllib.request.urlopen', side_effect=missing) as request, patch('feeds.common.time.sleep') as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                fetch('https://example.org')
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()


class LifecycleTests(unittest.TestCase):
    def test_discovery_and_edit(self):
        old = item(when=NOW - timedelta(days=2))
        changed = merge([old], [{'id': old['id'], 'content_text': 'Edited'}], NOW)[0]
        self.assertEqual(changed['date_published'], old['date_published'])
        self.assertEqual(changed['date_modified'], stamp(NOW))
        unchanged = merge([changed], [{'id': old['id'], 'content_text': 'Edited'}], NOW + timedelta(hours=3))[0]
        self.assertEqual(unchanged, changed)
        discovered = merge([], [{'id': 'new', 'content_text': 'New'}], NOW)[0]
        self.assertEqual(discovered['date_published'], stamp(NOW))

    def test_expiry(self):
        self.assertEqual(merge([item(when=NOW - timedelta(days=30, seconds=1))], [], NOW), [])
        self.assertEqual(len(merge([item(when=NOW - timedelta(days=30))], [], NOW)), 1)

    def test_validation(self):
        validate(history([item()]))
        with self.assertRaises(ValueError):
            validate(history([item(), item()]))
        broken = history([item()])
        broken['items'][0]['date_published'] = '2026-11-14'
        with self.assertRaises(ValueError):
            validate(broken)

    def test_bootstrap_only_on_explicit_404(self):
        error = urllib.error.HTTPError('https://example.org', 404, 'Missing', {}, None)
        with patch('feeds.__main__.fetch', side_effect=error):
            self.assertFalse(load(news, 'https://example.org', True)['_feeds']['initialized'])
            with self.assertRaises(urllib.error.HTTPError):
                load(news, 'https://example.org', False)
        with patch('feeds.__main__.fetch', return_value=(b'not json', 'text/html')):
            with self.assertRaises(ValueError):
                load(news, 'https://example.org', True)

    def test_errors_preserve_history_and_bootstrap(self):
        feed = history([item()], initialized=False)
        with patch.object(news, 'collect', side_effect=ValueError('Missing article author')):
            result, failures = generate(news, feed, NOW)
        self.assertEqual(len(failures), 1)
        self.assertFalse(result['_feeds']['initialized'])
        self.assertEqual(len(result['items']), 2)
        self.assertIn('Missing article author', next(x for x in result['items'] if x['id'].startswith('error:'))['content_text'])

    def test_fixed_icons_require_no_network_requests(self):
        for adapter in ADAPTERS:
            for old_icon in (None, 'https://example.org/old.png'):
                with self.subTest(adapter=adapter.SLUG, old_icon=old_icon):
                    feed = history(initialized=False)
                    if old_icon:
                        feed['icon'] = old_icon
                    with patch.object(adapter, 'collect', return_value=[]) as collect, patch('feeds.common.urllib.request.urlopen', side_effect=AssertionError('No icon requests')) as request:
                        result, failures = generate(adapter, feed, NOW)
                    self.assertTrue(result['_feeds']['initialized'])
                    self.assertEqual(result['icon'], adapter.ICON)
                    self.assertEqual(failures, [])
                    collect.assert_called_once()
                    request.assert_not_called()

    def test_diagnostic_redaction(self):
        self.assertNotIn('secret', diagnostic(ValueError('Bad https://example.org?sid=secret')))


class TrailTests(unittest.TestCase):
    def test_excludes_weather(self):
        self.assertEqual(trails.extract(trail_page()), ('Snow', 'November 14, 2026, 2:00pm'))
        self.assertEqual(trails.extract(trail_page(weather='Hot'))[0], 'Snow')

    def test_timestamp_change_only(self):
        feed = history([item('trails:old', 'Snow')])
        with patch.object(trails, 'page', return_value=trail_page(date='Changed')):
            self.assertEqual(trails.collect(feed, NOW), [])

    def test_reverting_text_is_new_event(self):
        feed = history()
        ids = []
        for index, text in enumerate(['A', 'B', 'A']):
            now = NOW + timedelta(hours=index * 3)
            with patch.object(trails, 'page', return_value=trail_page(text)):
                incoming = trails.collect(feed, now)
            ids.append(incoming[0]['id'])
            feed['items'] = merge(feed['items'], incoming, now)
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(len(feed['items']), 3)

    def test_layout_failure(self):
        with self.assertRaises(ValueError):
            trails.extract(Tree('<html>No update</html>').root)


class NewsTests(unittest.TestCase):
    def post(self, identifier=1):
        return {'id': identifier, 'title': {'rendered': 'Title &amp; More'}, 'content': {'rendered': '<p>Full content</p>'}, 'link': 'https://example.org/article', '_embedded': {'author': [{'name': 'Writer'}]}}

    def test_article(self):
        result = news.article(self.post())
        self.assertEqual(result['id'], 'news:1')
        self.assertEqual(result['title'], 'Title & More')
        self.assertEqual(result['authors'], [{'name': 'Writer'}])
        self.assertNotIn('date_published', result)

    def test_news_only_requests_collection_pages(self):
        feed = history([item('news:3')])
        responses = [(json.dumps([self.post(1)]).encode(), {'Content-Type': 'application/json', 'X-WP-TotalPages': '2'}), (json.dumps([self.post(2)]).encode(), {'Content-Type': 'application/json', 'X-WP-TotalPages': '2'})]
        with patch.object(news, 'fetch', side_effect=responses) as fetcher:
            result = news.collect(feed, NOW)
        self.assertEqual({x['id'] for x in result}, {'news:1', 'news:2'})
        self.assertEqual(fetcher.call_count, 2)
        self.assertTrue(all(call.args[0].startswith(news.API + '?') for call in fetcher.call_args_list))
        retained = merge(feed['items'], result, NOW)
        self.assertEqual(next(x for x in retained if x['id'] == 'news:3'), feed['items'][0])
        self.assertIn('page=2', fetcher.call_args.args[0])

    def test_initial_window_and_challenge(self):
        with patch.object(news, 'fetch', return_value=(b'[]', {'Content-Type': 'application/json', 'X-WP-TotalPages': '0'})) as fetcher:
            self.assertEqual(news.collect(history(initialized=False), NOW), [])
        self.assertIn('2026-11-07', fetcher.call_args.args[0])
        with patch.object(news, 'fetch', return_value=(b'<html>Captcha</html>', {'Content-Type': 'text/html'})):
            with self.assertRaises(ValueError):
                news.collect(history(), NOW)


class BulletinTests(unittest.TestCase):
    def test_original_only(self):
        first, created = bulletin.extract(topic_page(), 123)
        second, _ = bulletin.extract(topic_page(reply='Changed reply'), 123)
        self.assertEqual(first, second)
        self.assertEqual(first['content_text'], 'Original\nSecond line')
        self.assertEqual(first['authors'], [{'name': 'Creator'}])
        self.assertEqual(first['url'], 'https://methowvalley.com/bb/viewtopic.php?t=123')
        self.assertEqual(created.astimezone(UTC).hour, 22)
        edited, _ = bulletin.extract(topic_page(text='Edited'), 123)
        self.assertNotEqual(first, edited)

    def test_pagination_cutoff_and_retained_refresh(self):
        pages = [forum_page(2, 'Sat Nov 14, 2026 2:42 pm', 25), forum_page(1, 'Mon Nov 09, 2026 2:42 pm', 50), topic_page(), topic_page()]
        with patch.object(bulletin, 'page', side_effect=pages) as fetcher:
            result = bulletin.collect(history([item('topic:99')], initialized=False), NOW)
        self.assertEqual({x['id'] for x in result}, {'topic:2'})
        self.assertEqual(fetcher.call_count, 3)
        self.assertIn('sk=t', fetcher.call_args_list[0].args[0])
        self.assertIn('start=25', fetcher.call_args_list[1].args[0])


    def test_existing_topics_are_not_refetched(self):
        existing = item('topic:2', 'Original')
        with patch.object(bulletin, 'page', return_value=forum_page(2, 'Sat Nov 14, 2026 2:42 pm')) as fetcher:
            result = bulletin.collect(history([existing]), NOW)
        self.assertEqual(result, [])
        self.assertEqual(fetcher.call_count, 1)
        self.assertIn('viewforum.php', fetcher.call_args.args[0])
        self.assertEqual(merge([existing], result, NOW), [existing])



class StatusTests(unittest.TestCase):
    def test_one_entry_per_run_and_thirty_day_retention(self):
        old = item('status:old', 'Old run', NOW - timedelta(days=31))
        recent = item('status:recent', 'Recent run', NOW - timedelta(days=2))
        result = generate_status(history([old, recent]), [], ['News: source processing: HTTP 503'], NOW)
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(result['items'][1], recent)
        self.assertEqual(result['items'][0]['date_published'], stamp(NOW))
        self.assertEqual(result['items'][0]['title'], 'Feed update: 1 source error(s)')
        self.assertEqual(json.loads(result['items'][0]['content_text'])['failures'], ['News: source processing: HTTP 503'])
        validate(result)

    def test_legacy_status_is_migrated_and_missing_status_is_allowed(self):
        legacy = json.dumps({'source_errors': 0, 'failures': [], 'feed_items': {}}).encode()
        with patch('feeds.__main__.fetch', return_value=(legacy, 'application/json')):
            result = load_status('https://example.org')
        self.assertEqual(result['items'], [])
        self.assertFalse(result['_feeds']['initialized'])
        missing = urllib.error.HTTPError('https://example.org/status.json', 404, 'Missing', {}, None)
        with patch('feeds.__main__.fetch', side_effect=missing):
            self.assertEqual(load_status('https://example.org')['items'], [])
        with patch('feeds.__main__.fetch', return_value=(b'not json', 'application/json')):
            with self.assertRaises(ValueError):
                load_status('https://example.org')

    def test_status_counts_content_items(self):
        feeds = [('news', history([item('news:1'), item('error:1')]))]
        summary = json.loads(status_entry(feeds, [], NOW)['content_text'])
        self.assertEqual(summary, {'source_errors': 0, 'failures': [], 'feed_items': {'news': 1}})


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class CommandTests(unittest.TestCase):
    def test_reset_bypasses_history_and_rebuilds_with_new_discovery_times(self):
        seen = []

        def collect(feed, now):
            self.assertEqual(feed['items'], [])
            self.assertEqual(feed['_feeds'], {'initialized': False})
            seen.append(feed['title'])
            return [{'id': 'new:1', 'content_text': 'Current content'}]

        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            stack.enter_context(patch('sys.argv', ['feeds', '--base-url', 'https://example.org', '--output', directory, '--reset']))
            fetcher = stack.enter_context(patch('feeds.__main__.fetch', side_effect=AssertionError('Reset must not fetch history')))
            for adapter in ADAPTERS:
                (Path(directory) / (adapter.SLUG + '.json')).write_text(json.dumps(history([item('old:1', when=NOW - timedelta(days=2)), item('error:old')])))
                stack.enter_context(patch.object(adapter, 'collect', side_effect=collect))
            self.assertEqual(main(), 0)
            fetcher.assert_not_called()
            self.assertEqual(len(seen), 3)
            for adapter in ADAPTERS:
                result = json.loads((Path(directory) / (adapter.SLUG + '.json')).read_text())
                validate(result)
                self.assertEqual([entry['id'] for entry in result['items']], ['new:1'])
                self.assertTrue(result['_feeds']['initialized'])
                self.assertEqual(result['icon'], adapter.ICON)
                self.assertEqual(result['items'][0]['date_published'], result['_feeds']['last_successful_at'])


    def test_first_deployment_and_published_history_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=directory))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch('sys.argv', ['feeds', '--base-url', base, '--output', directory, '--bootstrap']))
                    for adapter in ADAPTERS:
                        stack.enter_context(patch.object(adapter, 'collect', return_value=[{'id': adapter.SLUG + ':1', 'content_text': 'Original'}]))
                    with patch('feeds.__main__.datetime') as clock:
                        clock.now.return_value = NOW
                        self.assertEqual(main(), 0)
                before = {adapter.SLUG: json.loads((Path(directory) / (adapter.SLUG + '.json')).read_text()) for adapter in ADAPTERS}
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch('sys.argv', ['feeds', '--base-url', base, '--output', directory]))
                    for adapter in ADAPTERS:
                        stack.enter_context(patch.object(adapter, 'collect', return_value=[{'id': adapter.SLUG + ':1', 'content_text': 'Original'}]))
                    with patch('feeds.__main__.datetime') as clock:
                        clock.now.return_value = NOW + timedelta(hours=3)
                        self.assertEqual(main(), 0)
                for adapter in ADAPTERS:
                    after = json.loads((Path(directory) / (adapter.SLUG + '.json')).read_text())
                    self.assertEqual(after['items'], before[adapter.SLUG]['items'])
                    validate(after)
                status = json.loads((Path(directory) / 'status.json').read_text())
                validate(status)
                self.assertEqual(len(status['items']), 2)
                self.assertEqual(json.loads(status['items'][0]['content_text'])['source_errors'], 0)
                (Path(directory) / (news.SLUG + '.json')).write_text('broken')
                with patch('sys.argv', ['feeds', '--base-url', base, '--output', directory]), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(), 1)
                self.assertEqual((Path(directory) / (news.SLUG + '.json')).read_text(), 'broken')
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
