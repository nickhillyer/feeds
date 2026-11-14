import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .common import Tree, normalize

URLS = (
    'https://methowvalleynews.com/wp-json/wp/v2/posts?per_page=1&_embed=1',
    'https://methowvalleynews.com/',
    'https://methowvalley.com/bb/viewforum.php?f=3&sk=t&sd=d&start=0',
    'https://methowvalley.com/favicon.ico',
)
AGENTS = (
    ('feeds', 'Feeds/1.0 (+https://github.com/nickhillyer/feeds)'),
    ('python', 'Python-urllib/3.12'),
    ('browser', 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'),
)


def inspect(arguments):
    url, (label, agent) = arguments
    result = {'url': url, 'agent': label}
    try:
        request = urllib.request.Request(url, headers={'User-Agent': agent, 'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read(1048576)
            result.update(status=response.status, content_type=response.headers.get('Content-Type'), encoding=response.headers.get('Content-Encoding'), bytes=len(data))
            text = data.decode('utf-8', errors='replace')
            root = Tree(text).root
            title = root.find(lambda n: n.tag == 'title')
            result['title'] = normalize(title.text())[:160] if title else None
            result['markers'] = [marker for marker in ('sgcaptcha', 'captcha', 'cloudflare', 'access denied', 'topictitle', 'topic-poster', 'wp-json', 'robot', 'blocked') if marker in text.lower()]
            result['json_array'] = text.lstrip().startswith('[')
    except urllib.error.HTTPError as error:
        result['status'] = error.code
    except Exception as error:
        result['error_type'] = type(error).__name__
    message = json.dumps(result).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print('::notice::Source diagnostic: ' + message, flush=True)


def main():
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(inspect, [(url, agent) for url in URLS for agent in AGENTS]))


if __name__ == '__main__':
    main()
