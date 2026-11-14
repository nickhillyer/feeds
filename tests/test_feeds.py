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
from feeds.__main__ import ADAPTERS, generate, load, main
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
        with patch.object(news, 'collect', side_effect=ValueError('Missing article author')), patch('feeds.__main__.discover_icon', return_value='https://example.org/icon.png'):
            result, failures = generate(news, feed, NOW)
        self.assertEqual(len(failures), 1)
        self.assertFalse(result['_feeds']['initialized'])
        self.assertEqual(len(result['items']), 2)
        self.assertIn('Missing article author', next(x for x in result['items'] if x['id'].startswith('error:'))['content_text'])

    def test_icon_failure_does_not_erase_success(self):
        feed = history(initialized=False)
        feed['icon'] = 'https://example.org/old.png'
        with patch.object(news, 'collect', return_value=[]), patch('feeds.__main__.discover_icon', side_effect=ValueError('No icon')):
            result, failures = generate(news, feed, NOW)
        self.assertTrue(result['_feeds']['initialized'])
        self.assertEqual(result['icon'], feed['icon'])
        self.assertEqual(len(failures), 1)

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

    def test_pagination_and_retained_refresh(self):
        feed = history([item('news:3')])
        responses = [(json.dumps([self.post(1)]).encode(), {'Content-Type': 'application/json', 'X-WP-TotalPages': '2'}), (json.dumps([self.post(2)]).encode(), {'Content-Type': 'application/json', 'X-WP-TotalPages': '2'})]
        with patch.object(news, 'fetch', side_effect=responses) as fetcher, patch.object(news, 'get_json', return_value=self.post(3)) as refresher:
            result = news.collect(feed, NOW)
        self.assertEqual({x['id'] for x in result}, {'news:1', 'news:2', 'news:3'})
        self.assertIn('page=2', fetcher.call_args.args[0])
        self.assertIn('/3?', refresher.call_args.args[0])

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
        self.assertEqual({x['id'] for x in result}, {'topic:2', 'topic:99'})
        self.assertEqual(fetcher.call_count, 4)
        self.assertIn('sk=t', fetcher.call_args_list[0].args[0])
        self.assertIn('start=25', fetcher.call_args_list[1].args[0])


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class CommandTests(unittest.TestCase):
    def test_first_deployment_and_published_history_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=directory))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch('sys.argv', ['feeds', '--base-url', base, '--output', directory, '--bootstrap']))
                    stack.enter_context(patch('feeds.__main__.discover_icon', return_value='https://example.org/icon.png'))
                    for adapter in ADAPTERS:
                        stack.enter_context(patch.object(adapter, 'collect', return_value=[{'id': adapter.SLUG + ':1', 'content_text': 'Original'}]))
                    self.assertEqual(main(), 0)
                before = {adapter.SLUG: json.loads((Path(directory) / (adapter.SLUG + '.json')).read_text()) for adapter in ADAPTERS}
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch('sys.argv', ['feeds', '--base-url', base, '--output', directory]))
                    stack.enter_context(patch('feeds.__main__.discover_icon', return_value='https://example.org/icon.png'))
                    for adapter in ADAPTERS:
                        stack.enter_context(patch.object(adapter, 'collect', return_value=[{'id': adapter.SLUG + ':1', 'content_text': 'Original'}]))
                    self.assertEqual(main(), 0)
                for adapter in ADAPTERS:
                    after = json.loads((Path(directory) / (adapter.SLUG + '.json')).read_text())
                    self.assertEqual(after['items'], before[adapter.SLUG]['items'])
                    validate(after)
                self.assertEqual(json.loads((Path(directory) / 'status.json').read_text())['source_errors'], 0)
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
