"""Offline behavioral checks; no real secrets or Jira mutations."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import read_issue as r

HOST = 'example.atlassian.net'
SECRET = b'synthetic-secret'


def encrypt(value, prefix, version, host=HOST):
    key = hashlib.pbkdf2_hmac('sha1', b'peanuts' if prefix == b'v10' else SECRET,
                              b'saltysalt', 1, 16)
    raw = (hashlib.sha256(host.encode()).digest() if version >= 24 else b'') + value.encode()
    padder = padding.PKCS7(128).padder()
    raw = padder.update(raw) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(b' ' * 16)).encryptor()
    return prefix + enc.update(raw) + enc.finalize()


def response(payload=None, status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers or {'Content-Type': 'application/json'})
    result._content = json.dumps(payload).encode()
    result._content_consumed = True
    return result


class ReaderTests(unittest.TestCase):
    def test_encryption_versions_and_cookie_formats(self):
        for prefix in (b'v10', b'v11'):
            for version in (23, 24):
                for value in ('short', 'value-with-utf8-é-and-more-than-32-characters'):
                    with self.subTest(prefix=prefix, version=version, value=value):
                        def password():
                            if prefix == b'v10':
                                self.fail('v10 must not access keyring')
                            return SECRET
                        self.assertEqual(r.decrypt_cookie(encrypt(value, prefix, version),
                                                          HOST, version, password), value)

    def test_wrong_host_wrong_key_and_bad_padding_rejected(self):
        encrypted = encrypt('session', b'v11', 24)
        for host, key, data in [(HOST, b'wrong', encrypted),
                                ('wrong.atlassian.net', SECRET, encrypted),
                                (HOST, SECRET, encrypted[:-1]),
                                (HOST, SECRET, b'v20unknown')]:
            with self.subTest(host=host, key=key, data=data):
                with self.assertRaises(r.ReaderError):
                    r.decrypt_cookie(data, host, 24, lambda: key)

    def test_cookie_scope_expiry_security_and_partition(self):
        now = 1000
        expired = int((now - 1 + r.EPOCH_DELTA) * 1000000)
        future = int((now + 100 + r.EPOCH_DELTA) * 1000000)
        rows = [(HOST, '/', 'active', 'value', b'', 1, future, 1, ''),
                ('.atlassian.net', '/', 'session', 'value', b'', 1, 0, 0, ''),
                (HOST, '/', 'expired', 'value', b'', 1, expired, 1, ''),
                (HOST, '/', 'partitioned', 'value', b'', 1, future, 1, 'https://other.test')]
        jar, skipped = r.cookie_jar(rows, 24, lambda: SECRET, now)
        cookies = {cookie.name: cookie for cookie in jar}
        self.assertEqual(set(cookies), {'active', 'session'})
        self.assertTrue(cookies['active'].secure)
        self.assertFalse(cookies['active'].domain_specified)
        self.assertEqual(cookies['active'].expires, now + 100)
        self.assertTrue(cookies['session'].domain_initial_dot)
        self.assertIsNone(cookies['session'].expires)
        self.assertEqual(skipped, 1)

    def test_wal_snapshot_reads_recent_cookie_without_changing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'Cookies'
            writer = sqlite3.connect(db)
            self.addCleanup(writer.close)
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('CREATE TABLE meta (key TEXT, value TEXT)')
            writer.execute("INSERT INTO meta VALUES ('version', '24')")
            writer.execute('CREATE TABLE cookies (host_key TEXT, path TEXT, name TEXT, value TEXT, '
                           'encrypted_value BLOB, is_secure INTEGER, expires_utc INTEGER, '
                           'has_expires INTEGER, top_frame_site_key TEXT)')
            for host in (HOST, '.' + HOST, '.atlassian.net', 'other.atlassian.net', 'evil-atlassian.test'):
                writer.execute('INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                               (host, '/', 'synthetic', 'value', b'', 1, 0, 0, ''))
            writer.commit()
            before = hashlib.sha256(db.read_bytes()).digest()
            version, rows = r.cookie_rows(db, HOST)
            self.assertEqual(version, 24)
            self.assertEqual({row[0] for row in rows}, {HOST, '.' + HOST, '.atlassian.net'})
            self.assertEqual(hashlib.sha256(db.read_bytes()).digest(), before)
            self.assertEqual(writer.execute('SELECT count(*) FROM cookies').fetchone()[0], 5)
            writer.close()

    def test_profile_discovery_numbered_network_and_browser_pairing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            profile = home / '.config/google-chrome/Profile 2'
            (profile / 'Network').mkdir(parents=True)
            (profile / 'Network/Cookies').touch()
            self.assertEqual(r.discover(home), [('chrome', profile, profile / 'Network/Cookies')])
            self.assertEqual(r.discover(home, str(profile)), r.discover(home))

    def test_unsafe_targets_rejected(self):
        for url in ('http://example.atlassian.net/browse/JS-1',
                    'https://evil.test/browse/JS-1',
                    'https://example.atlassian.net.evil.test/browse/JS-1',
                    'https://user:secret@example.atlassian.net/browse/JS-1',
                    'https://example.atlassian.net/browse/JS-1?next=evil',
                    'https://example.atlassian.net:8443/browse/JS-1'):
            with self.subTest(url=url), self.assertRaises(r.ReaderError):
                r.target(url)

    def reader(self):
        reader = r.JiraReader(HOST, 'JS-1', requests.cookies.RequestsCookieJar())
        self.addCleanup(reader.close)
        return reader

    def test_all_comments_across_multiple_server_limited_pages(self):
        reader = self.reader()
        pages = [response({'key': 'JS-1', 'fields': {}}),
                 response({'startAt': 0, 'total': 3, 'comments': [{'id': '1'}, {'id': '2'}]}),
                 response({'startAt': 2, 'total': 3, 'comments': [{'id': '3'}]})]
        with patch.object(reader.session, 'get', side_effect=pages) as get:
            data = reader.fetch()
        self.assertEqual([c['id'] for c in data['fields']['comment']['comments']], ['1', '2', '3'])
        self.assertEqual(get.call_args_list[-1].kwargs['params']['startAt'], 2)

    def test_empty_and_changing_comment_pages_fail(self):
        for page in ({'startAt': 0, 'total': 1, 'comments': []},
                     {'startAt': 1, 'total': 1, 'comments': [{'id': '1'}]},
                     {'startAt': 0, 'total': 2, 'comments': [{'id': '1'}, {'id': '1'}]}):
            reader = self.reader()
            with patch.object(reader.session, 'get', side_effect=[
                    response({'key': 'JS-1', 'fields': {}}), response(page)]):
                with self.assertRaises(r.ReaderError):
                    reader.fetch()

    def test_redirects_and_auth_failures_never_followed_or_retried(self):
        for status in (301, 302, 401, 403, 404):
            reader = self.reader()
            with patch.object(reader.session, 'get', return_value=response(status=status)) as get:
                with self.assertRaises(r.ReaderError):
                    reader.get(reader.issue_path)
                self.assertEqual(get.call_count, 1)
                self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_endpoint_guard_blocks_non_read_endpoints_before_network(self):
        reader = self.reader()
        with patch.object(reader.session, 'get') as get:
            for path in ('https://evil.test/', reader.issue_path + '/transitions',
                         reader.issue_path + '/comment/123', reader.issue_path + '?evil=1'):
                with self.assertRaises(r.ReaderError):
                    reader.get(path)
            get.assert_not_called()

    def test_retry_limit_and_rate_limit(self):
        reader = self.reader()
        with patch.object(reader.session, 'get', return_value=response(status=503)) as get, patch.object(r.time, 'sleep'):
            with self.assertRaises(r.ReaderError):
                reader.get(reader.issue_path)
            self.assertEqual(get.call_count, 3)
        with patch.object(reader.session, 'get', side_effect=[response(status=429, headers={'Retry-After': '0'}), response({'ok': True})]), patch.object(r.time, 'sleep'):
            self.assertEqual(reader.get(reader.issue_path), {'ok': True})
        with patch.object(reader.session, 'get', return_value=response(status=429, headers={'Retry-After': '60'})) as get:
            with self.assertRaises(r.ReaderError):
                reader.get(reader.issue_path)
            self.assertEqual(get.call_count, 1)

    def test_non_json_and_network_fail_closed_without_body_disclosure(self):
        reader = self.reader()
        for result in (response('private content', headers={'Content-Type': 'text/html'}), response([], status=200)):
            with patch.object(reader.session, 'get', return_value=result):
                with self.assertRaises(r.ReaderError) as error:
                    reader.get(reader.issue_path)
                self.assertNotIn('private content', str(error.exception))
        with patch.object(reader.session, 'get', side_effect=requests.Timeout('secret-token')):
            with self.assertRaises(r.ReaderError) as error:
                reader.get(reader.issue_path)
            self.assertNotIn('secret-token', str(error.exception))

    def test_adf_and_nullable_people(self):
        nodes = [{'type': 'text', 'text': 'link', 'marks': [{'type': 'link', 'attrs': {'href': 'https://example.test'}}]},
                 {'type': 'hardBreak'}, {'type': 'mention', 'attrs': {'text': '@Person'}}]
        self.assertEqual(r.parse_adf(nodes), 'link (https://example.test)\n@Person')
        self.assertEqual(r.parse_adf(None), '')
        table = {'type': 'table', 'content': [{'type': 'tableRow', 'content': [
            {'type': 'tableCell', 'content': [{'type': 'text', 'text': 'A'}]},
            {'type': 'tableCell', 'content': [{'type': 'text', 'text': 'B'}]}]}]}
        self.assertEqual(r.parse_adf(table).strip(), 'A | B')
        data = {'key': 'JS-1', 'fields': {'reporter': None, 'assignee': None, 'description': None,
                                        'comment': {'comments': [], 'total': 0}}}
        self.assertIn('Assignee: Unassigned', r.render(data))

    def binary(self, content, content_type='image/png'):
        result = response(headers={'Content-Type': content_type})
        result._content = content
        return result

    def reader_with_attachment(self, **overrides):
        reader = self.reader()
        attachment = {'id': '42', 'filename': 'mockup.png', 'mimeType': 'image/png', 'size': 4, **overrides}
        issue = {'key': 'JS-1', 'fields': {'attachment': [attachment]}}
        with patch.object(reader.session, 'get', side_effect=[
                response(issue), response({'startAt': 0, 'total': 0, 'comments': []})]):
            data = reader.fetch()
        return reader, data

    def test_attachment_downloads_from_tenant_without_redirect_and_private_file(self):
        reader, data = self.reader_with_attachment()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(reader.session, 'get', return_value=self.binary(b'\x89PNG')) as get:
            count = r.download_attachments(reader, data, Path(tmp) / 'out', [], 1024)
            saved = Path(data['fields']['attachment'][0]['localPath'])
            self.assertEqual((count, saved.name, saved.read_bytes()), (1, '42-mockup.png', b'\x89PNG'))
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            self.assertEqual(saved.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(get.call_args.args[0], f'https://{HOST}/rest/api/3/attachment/content/42')
            self.assertEqual(get.call_args.kwargs['params'], {'redirect': 'false'})
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
            self.assertIn('-> ' + str(saved), r.render({**data, 'fields': {**data['fields'], 'comment': {'comments': [], 'total': 0}}}))

    def test_attachment_guard_size_limit_and_existing_file(self):
        reader, data = self.reader_with_attachment(size=None)
        with patch.object(reader.session, 'get') as get:
            with self.assertRaises(r.ReaderError):
                reader.download({'id': '43', 'filename': 'other.png'}, Path('/nonexistent'), 1024)
            get.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(reader.session, 'get', return_value=self.binary(b'x' * 10)):
                with self.assertRaises(r.DownloadError):
                    r.download_attachments(reader, data, Path(tmp), [], 5)
            self.assertEqual(list(Path(tmp).iterdir()), [])
            (Path(tmp) / '42-mockup.png').write_bytes(b'keep')
            with patch.object(reader.session, 'get') as get:
                with self.assertRaises(r.DownloadError):
                    r.download_attachments(reader, data, Path(tmp), [], 1024)
                get.assert_not_called()
            self.assertEqual((Path(tmp) / '42-mockup.png').read_bytes(), b'keep')

    def test_attachment_html_login_page_and_unknown_selector_rejected(self):
        reader, data = self.reader_with_attachment()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(reader.session, 'get', return_value=self.binary(b'<html>', 'text/html')):
                with self.assertRaises(r.DownloadError):
                    r.download_attachments(reader, data, Path(tmp), [], 1024)
            self.assertEqual(list(Path(tmp).iterdir()), [])
            with self.assertRaises(r.DownloadError):
                r.download_attachments(reader, data, Path(tmp), ['missing.png'], 1024)

    def test_attachment_filenames_never_escape_directory(self):
        self.assertEqual(r.local_name({'id': '7', 'filename': '../../.bashrc'}), '7-bashrc')
        self.assertEqual(r.local_name({'id': '7', 'filename': 'a/b\\c:d.png'}), '7-b_c_d.png')
        for identity in ('../1', '0', 'abc', None):
            with self.assertRaises(r.ReaderError):
                r.attachment_path({'id': identity})

    def test_adf_media_shows_attachment_filename(self):
        node = {'type': 'mediaSingle', 'content': [{'type': 'media', 'attrs': {'id': 'uuid', 'alt': 'mockup.png'}}]}
        self.assertEqual(r.parse_adf(node), '[media: mockup.png]\n')


if __name__ == '__main__':
    unittest.main()
