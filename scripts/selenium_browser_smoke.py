"""Opt-in, bounded legacy Selenium smoke against a local HTML fixture only.

Selenium Manager may obtain the matching driver. No account configuration or
cookies are read, and Chrome runs with an isolated temporary profile and DNS
disabled for external hosts. This is deliberately outside the unit test suite.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


def _worker_isolated() -> dict:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from xuse.features.publisher import composer
    from xuse.features.scraper.parsing import find_article_with_status_id
    from xuse.models import AccountConfig

    html = """<!doctype html><meta charset="utf-8"><title>Local Selenium fixture</title>
    <article id="outer"><a href="https://x.com/outer/status/999"><time>now</time></a>
      <div role="link"><a href="https://x.com/quoted/status/123"><time>then</time></a></div>
      <a href="https://x.com/quoted/status/123">mentioned post</a></article>
    <a data-testid="SideNav_NewTweet_Button" href="#">Post</a>
    <div data-testid="layers">
      <div data-testid="tweetTextarea_0" role="textbox" contenteditable="true">old draft</div>
      <button data-testid="tweetButton" onclick="submitPost()">Post</button>
    </div>
    <script>
      window.submitted = 0;
      function submitPost() {
        window.submitted++;
        document.querySelector('[data-testid=tweetTextarea_0]').remove();
        const toast = document.createElement('div');
        toast.dataset.testid = 'toast';
        toast.textContent = new URLSearchParams(location.search).has('reject') ? 'Draft saved' : 'Your post was sent.';
        document.body.appendChild(toast);
      }
    </script>"""

    with tempfile.TemporaryDirectory(prefix='xuse-selenium-smoke-', ignore_cleanup_errors=True) as tmp:
        fixture = Path(tmp) / 'fixture.html'
        fixture.write_text(html, encoding='utf-8')
        options = Options()
        options.add_argument('--headless=new')
        options.add_argument('--disable-background-networking')
        options.add_argument('--disable-sync')
        options.add_argument('--no-first-run')
        options.add_argument('--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost')
        options.add_argument(f'--user-data-dir={Path(tmp) / "profile"}')
        os.environ.setdefault('SE_TIMEOUT', '30')
        driver = None
        try:
            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(15)
            driver.set_script_timeout(10)
            driver.get(fixture.as_uri())
            assert find_article_with_status_id(driver, '123') is None, 'quoted post selected as outer target'
            article = find_article_with_status_id(driver, '999')
            assert article is not None and article.get_attribute('id') == 'outer'

            class LocalDriver:
                """Redirect the production composer's navigation to our fixture."""
                reject = False

                def __getattr__(self, name):
                    return getattr(driver, name)

                def get(self, _url):
                    driver.get(fixture.as_uri() + ('?reject=1' if self.reject else ''))

            local = LocalDriver()

            class LocalManager:
                def get_driver(self):
                    return local

            account = AccountConfig(account_id='synthetic-local-smoke')
            assert composer.post_new_tweet(LocalManager(), account, 'Synthetic local fixture text', []) is True
            assert driver.execute_script('return window.submitted') == 1
            local.reject = True
            assert composer.post_new_tweet(LocalManager(), account, 'Synthetic rejected fixture text', []) is False
            assert driver.execute_script('return window.submitted') == 1
            assert driver.current_url.startswith('file:'), 'browser navigated outside local fixture'
            return {
                'status': 'passed', 'checks': 6,
                'browser_version': driver.capabilities.get('browserVersion'),
                'driver_version': driver.capabilities.get('chrome', {}).get('chromedriverVersion', '').split(' ')[0],
                'external_actions': 0, 'fixture_navigation': 'file-only',
                'coverage': ['own-timestamp selection', 'quoted-target rejection',
                             'real editor and submit click', 'fresh success toast',
                             'detached editor plus unrelated toast rejected', 'one submission per scenario'],
            }
        finally:
            if driver is not None:
                driver.quit()


def _worker() -> dict:
    # Isolate even import-time legacy ConfigLoader/logger initialization.
    # Do this before importing any xuse module so a developer's accounts and
    # settings are never loaded by this opt-in smoke test.
    with tempfile.TemporaryDirectory(prefix='xuse-selenium-state-', ignore_cleanup_errors=True) as tmp:
        os.environ['X_USE_HOME'] = tmp
        return _worker_isolated()


def _run_worker(cmd: list[str], timeout: int, *, platform: str | None = None) -> dict:
    windows = (platform or sys.platform) == 'win32'
    creationflags = subprocess.CREATE_NO_WINDOW if windows else 0
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=creationflags, start_new_session=not windows,
    )
    try:
        stdout, stderr = process.communicate(timeout=max(1, min(timeout, 180)))
        try:
            return json.loads(stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {'status': 'failed', 'error_type': 'WorkerFailure', 'detail': stderr[-600:]}
    except subprocess.TimeoutExpired:
        result = {'status': 'unavailable', 'error_type': 'BoundedStartupOrSmokeTimeout', 'external_actions': 0}
        # POSIX descendants inherit the worker's private session/process group.
        # Its PID is the group ID because Popen used start_new_session=True.
        # Neither branch enumerates or signals unrelated browser processes.
        try:
            if windows:
                subprocess.run(
                    ['taskkill', '/PID', str(process.pid), '/T', '/F'],
                    capture_output=True, check=False, timeout=10,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # The owned process group already exited during cleanup.
        except (OSError, subprocess.TimeoutExpired) as exc:
            result['cleanup_error'] = type(exc).__name__
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            # A descendant that escaped the group can retain inherited pipes.
            # Do not block forever or search for unrelated processes to kill.
            process.kill()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            result['cleanup_error'] = 'CleanupTimeout'
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--timeout', type=int, default=120)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.worker:
        try:
            result = _worker()
        except Exception as exc:
            result = {'status': 'failed', 'error_type': type(exc).__name__, 'detail': str(exc)[:600], 'external_actions': 0}
        print(json.dumps(result))
        return 0 if result['status'] == 'passed' else 1

    cmd = [sys.executable, str(Path(__file__).resolve()), '--worker']
    result = _run_worker(cmd, args.timeout)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
