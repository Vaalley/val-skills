#!/usr/bin/env python3
"""Read a Jira Cloud issue using Linux Chromium cookies. No Jira writes."""
import argparse
from contextlib import closing
import hashlib
import http.cookiejar
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

EPOCH_DELTA = 11644473600
BROWSERS = (
    ('brave', '.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser'),
    ('brave', '.config/BraveSoftware/Brave-Browser'),
    ('chrome', '.config/google-chrome'),
    ('chromium', '.config/chromium'),
    ('chrome', '.var/app/com.google.Chrome/config/google-chrome'),
    ('chromium', '.var/app/org.chromium.Chromium/config/chromium'),
)


class ReaderError(Exception):
    pass


class DownloadError(ReaderError):
    pass


def attachment_path(attachment):
    identity = str((attachment or {}).get('id', ''))
    if not re.fullmatch(r'[1-9][0-9]{0,18}', identity):
        raise ReaderError('Invalid Jira attachment id.')
    return '/rest/api/3/attachment/content/' + identity


def local_name(attachment):
    # Never trust the server filename as a path: keep its basename, strip unsafe characters, prefix the id.
    name = re.sub(r'[^\w.\- ]', '_', Path(str(attachment.get('filename') or 'attachment')).name).strip(' .')
    return f"{attachment['id']}-{name[:150] or 'attachment'}"


def target(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment
            or not re.fullmatch(r'[a-z0-9][a-z0-9-]*\.atlassian\.net', parsed.hostname or '')):
        raise ReaderError('Use an HTTPS Jira Cloud browse URL without credentials, query, or fragment.')
    match = re.fullmatch(r'/browse/([A-Z][A-Z0-9_]*-[1-9][0-9]*)/?', parsed.path)
    if not match:
        raise ReaderError('Expected /browse/PROJECT-123.')
    return parsed.hostname, match[1]


def discover(home, selected=None, browser=None):
    if selected:
        profile = Path(selected).expanduser().resolve()
        inferred = [app for app, root in BROWSERS if profile.parent == (home / root).resolve()]
        app = browser or (inferred[0] if inferred else None)
        if not app:
            raise ReaderError('Custom profile requires --browser brave, chrome, or chromium.')
        profiles = [(app, profile)]
    else:
        profiles = []
        for app, root in BROWSERS:
            if browser and app != browser:
                continue
            base = home / root
            profiles.extend((app, p) for p in sorted(base.glob('*'))
                            if p.is_dir() and (p.name == 'Default' or re.fullmatch(r'Profile \d+', p.name)))
    return [(app, p, db) for app, p in profiles
            for db in (p / 'Cookies', p / 'Network/Cookies') if db.is_file()]


def cookie_rows(db, host):
    # A read-only transaction is a WAL-aware consistent snapshot. No full DB copy.
    with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        version = int(conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0])
        columns = {r[1] for r in conn.execute('PRAGMA table_info(cookies)')}
        partition = 'top_frame_site_key' if 'top_frame_site_key' in columns else "''"
        rows = conn.execute(
            f'SELECT host_key, path, name, value, encrypted_value, is_secure, '
            f'expires_utc, has_expires, {partition} FROM cookies '
            'WHERE host_key IN (?, ?, ?)', (host, '.' + host, '.atlassian.net')).fetchall()
        conn.rollback()
    return version, rows


def storage_password(browser):
    try:
        result = subprocess.run(['secret-tool', 'lookup', 'application', browser],
                                capture_output=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ReaderError(f'Cannot access {browser} Safe Storage via secret-tool.') from None
    password = result.stdout.rstrip(b'\r\n')
    if result.returncode != 0 or not password:
        raise ReaderError(f'No {browser} Safe Storage secret; unlock the keyring or use another profile.')
    return password


def decrypt_cookie(encrypted, host, version, password):
    prefix = encrypted[:3]
    if prefix not in (b'v10', b'v11'):
        raise ReaderError('Unsupported cookie encryption prefix; use the logged-in browser instead.')
    secret = b'peanuts' if prefix == b'v10' else password()
    key = hashlib.pbkdf2_hmac('sha1', secret, b'saltysalt', 1, 16)
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(b' ' * 16)).decryptor()
        raw = decryptor.update(encrypted[3:]) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        raw = unpadder.update(raw) + unpadder.finalize()
        if version >= 24:
            if len(raw) < 32 or raw[:32] != hashlib.sha256(host.encode()).digest():
                raise ReaderError('Cookie host hash mismatch; wrong key or corrupt cookie.')
            raw = raw[32:]
        return raw.decode('utf-8')
    except (ValueError, UnicodeError):
        raise ReaderError('Cookie decryption failed; wrong key or corrupt cookie.') from None


def cookie_jar(rows, version, password, now=None):
    now = time.time() if now is None else now
    jar = requests.cookies.RequestsCookieJar()
    skipped_partitioned = 0
    for host, path, name, value, encrypted, secure, expires, has_expires, partition in rows:
        if partition:
            skipped_partitioned += 1
            continue
        expiry = expires / 1000000 - EPOCH_DELTA if has_expires else None
        if expiry is not None and expiry <= now:
            continue
        value = decrypt_cookie(encrypted, host, version, password) if encrypted else value
        if not value:
            continue
        domain_cookie = host.startswith('.')
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=host, domain_specified=domain_cookie, domain_initial_dot=domain_cookie,
            path=path, path_specified=True, secure=bool(secure), expires=expiry,
            discard=expiry is None, comment=None, comment_url=None, rest={}))
    return jar, skipped_partitioned


class JiraReader:
    def __init__(self, host, issue, cookies):
        self.origin = 'https://' + host
        self.issue_path = '/rest/api/3/issue/' + quote(issue, safe='')
        self.session = requests.Session()
        self.session.trust_env = False  # Do not silently load .netrc credentials.
        self.session.cookies = cookies
        self.session.headers.update({'Accept': 'application/json', 'User-Agent': 'jira-read-only/1.0'})
        self.requests = 0
        # Attachment content paths are allowed only once the issue's own metadata listed them.
        self.attachment_paths = set()

    def close(self):
        self.session.close()

    def request(self, path, params=None, stream=False):
        # Exact endpoint allowlist: no arbitrary URL, method, or redirect escape.
        if path not in (self.issue_path, self.issue_path + '/comment') and path not in self.attachment_paths:
            raise ReaderError('Read-only endpoint guard rejected the request.')
        for attempt in range(3):
            self.requests += 1
            try:
                response = self.session.get(self.origin + path, params=params, stream=stream,
                                            timeout=(5, 20), allow_redirects=False)
            except requests.RequestException:
                raise ReaderError('Jira GET failed (network/TLS/timeout); no credentials logged.') from None
            status = response.status_code
            if status in (429, 502, 503, 504) and attempt < 2:
                response.close()
                delay = response.headers.get('Retry-After', '')
                if delay.isdigit() and int(delay) > 5:
                    raise ReaderError('Jira requested a longer cooldown; retry later.')
                time.sleep(int(delay) if delay.isdigit() else 0.5 * (attempt + 1))
                continue
            if status != 200:
                response.close()
            if 300 <= status < 400:
                raise ReaderError('Jira redirected the request; sign in using the selected browser profile.')
            explanations = {401: 'session rejected; sign in again',
                            403: 'access forbidden; check issue permissions and session',
                            404: 'issue or attachment not found, or hidden by permissions'}
            if status != 200:
                raise ReaderError(f'Jira HTTP {status}: {explanations.get(status, "read request failed")}.')
            return response
        raise ReaderError('Jira retry limit reached.')

    def get(self, path, params=None):
        with self.request(path, params) as response:
            if 'application/json' not in response.headers.get('Content-Type', '').lower():
                raise ReaderError('Jira returned non-JSON content; check browser login.')
            try:
                data = response.json()
            except ValueError:
                raise ReaderError('Jira returned invalid JSON.') from None
            if not isinstance(data, dict):
                raise ReaderError('Jira returned an unexpected JSON shape.')
            return data

    def download(self, attachment, directory, max_bytes):
        """Stream one attachment of this issue into `directory`; never overwrites a file."""
        path = attachment_path(attachment)
        declared = attachment.get('size')
        if isinstance(declared, int) and declared > max_bytes:
            raise ReaderError(f'Attachment {attachment["id"]} exceeds the size limit; raise --max-attachment-mb.')
        destination = directory / local_name(attachment)
        partial = destination.with_name(destination.name + '.part')
        if destination.exists() or partial.exists():
            raise ReaderError(f'{destination.name} already exists in {directory}; choose another --attachments-dir.')
        # redirect=false: Jira streams the bytes itself instead of redirecting to its media host,
        # so the session cookies never leave the tenant origin.
        with self.request(path, {'redirect': 'false'}, stream=True) as response:
            content_type = response.headers.get('Content-Type', '').lower()
            if 'text/html' in content_type and 'html' not in str(attachment.get('mimeType', '')).lower():
                raise ReaderError('Jira returned an HTML page instead of the attachment; check browser login.')
            written = 0
            created = False
            try:
                with open(partial, 'xb') as file:
                    created = True
                    partial.chmod(0o600)
                    for chunk in response.iter_content(64 * 1024):
                        written += len(chunk)
                        if written > max_bytes:
                            raise ReaderError(f'Attachment {attachment["id"]} exceeds the size limit; raise --max-attachment-mb.')
                        file.write(chunk)
                if isinstance(declared, int) and written != declared:
                    raise ReaderError(f'Attachment {attachment["id"]} is incomplete: {written} of {declared} bytes.')
                partial.rename(destination)
            except requests.RequestException:
                raise ReaderError('Attachment download failed (network/TLS/timeout).') from None
            finally:
                if created and partial.exists():
                    partial.unlink()
        return destination

    def fetch(self):
        data = self.get(self.issue_path, {'fields': 'summary,status,reporter,assignee,description,attachment'})
        attachments = data.get('fields', {}).get('attachment') or []
        if not isinstance(attachments, list):
            raise ReaderError('Invalid Jira attachment metadata.')
        for attachment in attachments:
            self.attachment_paths.add(attachment_path(attachment))
        comments = []
        seen = set()
        start = 0
        for _ in range(1000):
            page = self.get(self.issue_path + '/comment',
                            {'startAt': start, 'maxResults': 100, 'orderBy': 'created'})
            batch = page.get('comments')
            total = page.get('total')
            if not isinstance(batch, list) or not isinstance(total, int) or total < 0:
                raise ReaderError('Invalid Jira comment pagination response.')
            if page.get('startAt') != start:
                raise ReaderError('Jira returned an unexpected comment page offset.')
            for comment in batch:
                identity = comment.get('id')
                if not identity or identity in seen:
                    raise ReaderError('Comments changed during pagination; retry for a consistent result.')
                seen.add(identity)
                comments.append(comment)
            start += len(batch)
            if start >= total:
                if len(comments) != total:
                    raise ReaderError('Comment total changed during pagination; retry.')
                data['fields']['comment'] = {'comments': comments, 'total': total}
                return data
            if not batch:
                raise ReaderError(f'Incomplete comments: retrieved {len(comments)} of {total}.')
        raise ReaderError('Comment pagination exceeded safety limit.')


def parse_adf(node):
    if node is None:
        return ''
    if isinstance(node, list):
        return ''.join(parse_adf(item) for item in node)
    if not isinstance(node, dict):
        return str(node)
    kind = node.get('type')
    attrs = node.get('attrs') or {}
    if kind == 'text':
        text = node.get('text', '')
        for mark in node.get('marks', []):
            if mark.get('type') == 'link':
                href = (mark.get('attrs') or {}).get('href', '')
                if href and href != text:
                    text += f' ({href})'
        return text
    if kind == 'hardBreak':
        return '\n'
    if kind == 'mention':
        return attrs.get('text') or '@' + attrs.get('id', 'unknown')
    if kind in ('inlineCard', 'blockCard'):
        return attrs.get('url', '') + ('\n' if kind == 'blockCard' else '')
    if kind == 'emoji':
        return attrs.get('text') or attrs.get('shortName', '')
    if kind == 'media':
        # Media ids are not attachment ids: the alt text is the attachment filename.
        return f"[media: {attrs.get('alt') or attrs.get('id', 'unknown')}]\n"
    if kind == 'tableRow':
        return ' | '.join(parse_adf(cell).strip() for cell in node.get('content', [])) + '\n'
    if kind == 'orderedList':
        return ''.join(f'{i}. {parse_adf(item.get("content", [])).strip()}\n'
                       for i, item in enumerate(node.get('content', []), attrs.get('order', 1)))
    text = parse_adf(node.get('content', []))
    if kind in ('paragraph', 'heading', 'blockquote', 'table'):
        return text + '\n'
    if kind == 'listItem':
        return '- ' + text.rstrip() + '\n'
    if kind == 'codeBlock':
        fence = '```'
        while fence in text:
            fence += '`'
        return fence + attrs.get('language', '') + '\n' + text + '\n' + fence + '\n'
    if kind == 'rule':
        return '---\n'
    return text


def render(data):
    fields = data['fields']
    person = lambda value: (value or {}).get('displayName') or 'Unassigned'
    lines = [f"[{data['key']}] {fields.get('summary', '')}",
             f"Status: {(fields.get('status') or {}).get('name', '')}",
             f"Reporter: {person(fields.get('reporter'))}",
             f"Assignee: {person(fields.get('assignee'))}",
             '\n--- DESCRIPTION ---', parse_adf(fields.get('description')).rstrip()]
    comments = fields['comment']
    lines.append(f"\n--- COMMENTS ({len(comments['comments'])}/{comments['total']}) ---")
    for comment in comments['comments']:
        lines.extend([f"\n[{comment.get('created', '')}] {person(comment.get('author'))}:",
                      parse_adf(comment.get('body')).rstrip()])
    attachments = fields.get('attachment') or []
    lines.append(f'\n--- ATTACHMENTS ({len(attachments)}) ---')
    for attachment in attachments:
        saved = f" -> {attachment['localPath']}" if attachment.get('localPath') else ''
        lines.append(f"[{attachment.get('id')}] {attachment.get('filename', '')} "
                     f"({attachment.get('mimeType', 'unknown type')}, {attachment.get('size', '?')} bytes){saved}")
    return '\n'.join(lines)


def select_attachments(attachments, selectors):
    if not selectors:
        return list(attachments)
    unmatched = [s for s in selectors if not any(s in (str(a.get('id')), a.get('filename')) for a in attachments)]
    if unmatched:
        raise DownloadError('No attachment matches: ' + ', '.join(unmatched))
    return [a for a in attachments if str(a.get('id')) in selectors or a.get('filename') in selectors]


def download_attachments(reader, data, directory, selectors, max_bytes):
    attachments = data['fields'].get('attachment') or []
    selected = select_attachments(attachments, selectors)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for attachment in selected:
            attachment['localPath'] = str(reader.download(attachment, directory, max_bytes))
    except DownloadError:
        raise
    except ReaderError as error:
        raise DownloadError(str(error)) from None
    except OSError as error:
        raise DownloadError(f'Cannot write attachment: {type(error).__name__}.') from None
    return len(selected)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--profile', help='Browser profile directory, not the Cookies file')
    parser.add_argument('--browser', choices=['brave', 'chrome', 'chromium'])
    parser.add_argument('--json', action='store_true', help='Preserve raw ADF and issue fields')
    parser.add_argument('--attachments-dir', type=Path,
                        help='Download the issue attachments into this directory (created 0700, files 0600)')
    parser.add_argument('--attachment', action='append', default=[], metavar='ID_OR_FILENAME',
                        help='Download only this attachment (repeatable); requires --attachments-dir')
    parser.add_argument('--max-attachment-mb', type=float, default=50, help='Per-attachment size limit (default 50)')
    args = parser.parse_args(argv)
    if args.attachment and not args.attachments_dir:
        parser.error('--attachment requires --attachments-dir')
    if args.max_attachment_mb <= 0:
        parser.error('--max-attachment-mb must be positive')
    started = time.monotonic()
    try:
        host, issue = target(args.url)
        candidates = discover(Path.home(), args.profile, args.browser)
        if not candidates:
            raise ReaderError('No supported browser Cookies database found.')
        passwords = {}
        failures = []
        for browser, profile, db in candidates:
            reader = None
            try:
                version, rows = cookie_rows(db, host)
                if not rows:
                    continue
                def password():
                    if browser not in passwords:
                        passwords[browser] = storage_password(browser)
                    return passwords[browser]
                jar, partitioned = cookie_jar(rows, version, password)
                if not jar:
                    continue
                reader = JiraReader(host, issue, jar)
                data = reader.fetch()
                downloaded = 0
                if args.attachments_dir:
                    downloaded = download_attachments(reader, data, args.attachments_dir.expanduser(), args.attachment,
                                                      int(args.max_attachment_mb * 1024 * 1024))
                print(json.dumps(data, ensure_ascii=False) if args.json else render(data))
                print(f'Read-only GET: {reader.requests} requests; {time.monotonic() - started:.2f}s; '
                      f'profile {browser}/{profile.name}; attachments downloaded: {downloaded}; '
                      f'skipped partitioned cookies: {partitioned}', file=sys.stderr)
                return 0
            except DownloadError:
                # The session works: another profile would not fix a download failure.
                raise
            except (ReaderError, sqlite3.Error, OSError, TypeError, KeyError, ValueError) as error:
                # Never include database contents, HTTP bodies, or keyring output.
                message = str(error) if isinstance(error, ReaderError) else type(error).__name__
                failures.append(f'{browser}/{profile.name}: {message}')
            finally:
                if reader:
                    reader.close()
        raise ReaderError('No usable Jira session. ' + ('; '.join(failures) or
                          'No unexpired, unpartitioned tenant cookies; sign in or choose --profile.'))
    except (ReaderError, ValueError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
