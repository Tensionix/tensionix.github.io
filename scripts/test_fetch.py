"""Offline regression tests: adaptive GraphQL pages never become partial builds."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import fetch


def page(names, more=False, cursor=None, **profile):
    return {'data': {'user': {'login': 'Tensionix', 'bio': 'first profile', **profile,
                             'repositories': {'nodes': [{'name': name} for name in names],
                                              'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}}}}


def http_error(status):
    return urllib.error.HTTPError(fetch.GRAPHQL_URL, status, 'fixture', {}, None)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch.object(fetch.time, 'sleep').start()
        self.addCleanup(patch.stopall)
        self.stderr = io.StringIO()
        self.redirect = contextlib.redirect_stderr(self.stderr)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.calls = []

    def account(self, effects):
        effects = iter(effects)

        def urlopen(request, **kwargs):
            payload = json.loads(request.data)
            self.calls.append(payload)
            effect = next(effects)
            if isinstance(effect, Exception):
                raise effect
            return io.BytesIO(effect if isinstance(effect, bytes) else json.dumps(effect).encode())

        with patch.object(fetch.urllib.request, 'urlopen', side_effect=urlopen):
            return fetch.fetch_account('fixture-token-not-a-secret')

    def sizes(self):
        return [call['variables']['first'] for call in self.calls]

    def cursors(self):
        return [call['variables']['after'] for call in self.calls]

    def test_query_uses_page_size_variable(self):
        self.assertIn('$first: Int!', fetch.QUERY)
        self.assertIn('repositories(first: $first, after: $after', fetch.QUERY)

    def test_normal_multi_page_collection(self):
        result = self.account([page(['A','B'], True, 'cursor-b'), page(['C'])])
        self.assertEqual([r['name'] for r in result['repositories']], ['A','B','C'])
        self.assertEqual(self.sizes(), [10,10])
        self.assertEqual(self.cursors(), [None,'cursor-b'])

    def test_502_reduces_10_then_5_then_1_immediately(self):
        result = self.account([http_error(502), http_error(502),
                               page(['A'], True, 'cursor-a'), page(['B'])])
        self.assertEqual(self.sizes(), [10,5,1,1])
        self.assertEqual(self.cursors(), [None,None,None,'cursor-a'])
        self.assertEqual([r['name'] for r in result['repositories']], ['A','B'])
        self.sleep.assert_not_called()
        self.assertIn('10 -> 5', self.stderr.getvalue())
        self.assertIn('5 -> 1', self.stderr.getvalue())
        self.assertNotIn('fixture-token', self.stderr.getvalue())

    def test_recovery_on_5_keeps_smaller_size(self):
        result = self.account([http_error(502), page(['A'], True, 'a'), page(['B'])])
        self.assertEqual(self.sizes(), [10,5,5])
        self.assertEqual(len(result['repositories']), 2)

    def test_later_failure_preserves_completed_pages_and_cursor(self):
        result = self.account([page(['A'], True, 'a'), http_error(502), http_error(502),
                               page(['B'], True, 'b'), page(['C'])])
        self.assertEqual(self.sizes(), [10,10,5,1,1])
        self.assertEqual(self.cursors(), [None,'a','a','a','b'])
        self.assertEqual([r['name'] for r in result['repositories']], ['A','B','C'])

    def test_single_repository_page_retries_then_fails(self):
        with self.assertRaises(fetch.FetchError) as raised:
            self.account([http_error(502) for _ in range(6)])
        self.assertEqual(raised.exception.status, 502)
        self.assertEqual(self.sizes(), [10,5,1,1,1,1])
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [2,4,8])

    def test_authentication_error_does_not_shrink(self):
        with self.assertRaises(fetch.FetchError) as raised:
            self.account([http_error(403)])
        self.assertEqual(raised.exception.status,403)
        self.assertEqual(self.sizes(),[10])
        self.sleep.assert_not_called()

    def test_rate_limit_retains_normal_retry_policy(self):
        self.account([http_error(429),page(['A'])])
        self.assertEqual(self.sizes(),[10,10])
        self.sleep.assert_called_once_with(2)

    def test_other_server_errors_do_not_trigger_size_fallback(self):
        with self.assertRaises(fetch.FetchError) as raised:
            self.account([http_error(503) for _ in range(4)])
        self.assertEqual(raised.exception.status,503)
        self.assertEqual(self.sizes(),[10]*4)

    def test_network_error_retains_normal_retry_policy(self):
        self.account([urllib.error.URLError('fixture'),page(['A'])])
        self.assertEqual(self.sizes(),[10,10])
        self.sleep.assert_called_once_with(2)

    def test_graphql_errors_with_partial_data_are_fatal(self):
        partial=page(['A']); partial['errors']=[{'message':'resource limit'}]
        with self.assertRaisesRegex(fetch.FetchError,'resource limit'):
            self.account([partial])
        self.assertEqual(self.sizes(),[10])

    def test_invalid_json_is_fatal(self):
        with self.assertRaisesRegex(fetch.FetchError,'invalid JSON'):
            self.account([b'<html>upstream error</html>'])

    def test_missing_data_is_fatal(self):
        for response in [[], {}, {'data':None}, {'data':[]}]:
            with self.subTest(response=response), self.assertRaises(fetch.FetchError):
                self.account([response])

    def test_missing_user_is_fatal(self):
        with self.assertRaises(fetch.FetchError):
            self.account([{'data':{'user':None}}])

    def test_malformed_repository_pages_are_fatal(self):
        for connection in [None, {}, {'nodes':None}, {'nodes':[],'pageInfo':None},
                           {'nodes':[],'pageInfo':{'hasNextPage':'false'}},
                           {'nodes':[None],'pageInfo':{'hasNextPage':False}},
                           {'nodes':[{}],'pageInfo':{'hasNextPage':False}}]:
            response=page([]); response['data']['user']['repositories']=connection
            with self.subTest(connection=connection), self.assertRaises(fetch.FetchError):
                self.account([response])

    def test_empty_nonfinal_page_is_fatal(self):
        with self.assertRaisesRegex(fetch.FetchError,'did not advance'):
            self.account([page([],True,'cursor')])

    def test_missing_cursor_is_fatal(self):
        for cursor in (None,'',7):
            with self.subTest(cursor=cursor), self.assertRaisesRegex(fetch.FetchError,'did not advance'):
                self.account([page(['A'],True,cursor)])

    def test_repeated_cursor_is_fatal(self):
        with self.assertRaisesRegex(fetch.FetchError,'did not advance'):
            self.account([page(['A'],True,'a'),page(['B'],True,'a')])

    def test_duplicate_repository_is_fatal(self):
        with self.assertRaisesRegex(fetch.FetchError,'duplicate repository'):
            self.account([page(['A'],True,'a'),page(['a'])])
        with self.assertRaisesRegex(fetch.FetchError,'duplicate repository'):
            self.account([page(['A','A'])])

    def test_fallback_does_not_accept_oversized_page(self):
        with self.assertRaisesRegex(fetch.FetchError,'oversized'):
            self.account([http_error(502),http_error(502),page(['A','B'])])

    def test_asset_requests_keep_502_retries(self):
        with patch.object(fetch.urllib.request,'urlopen',side_effect=[http_error(502),io.BytesIO(b'image')]) as open_:
            self.assertEqual(fetch.request('https://example.test/icon.png'),b'image')
            self.assertEqual(open_.call_count,2)
        self.sleep.assert_called_once_with(2)

    def test_optional_missing_asset_behavior_unchanged(self):
        with patch.object(fetch.urllib.request,'urlopen',side_effect=http_error(404)):
            self.assertIsNone(fetch.request('https://example.test/icon.png',missing_ok=True))

    def test_terminal_failure_does_not_build_save_or_modify_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            protected=[root/'_data/github.json',root/'media/existing.png',root/'_project_pages/existing.md']
            for path in protected:
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(b'previous good build')
            saved=root/'saved-response.json'
            effects=[page(['A'],True,'a')['data']]+[fetch.FetchError('HTTP 502',status=502) for _ in range(3)]
            with patch.object(fetch,'graphql',side_effect=effects),patch.object(fetch,'build') as build,\
                 patch.object(fetch,'DATA_FILE',protected[0]),patch.object(fetch,'MEDIA',root/'media'),\
                 patch.object(fetch,'PAGES',root/'_project_pages'),patch.dict(os.environ,{'GITHUB_TOKEN':'fixture'}),\
                 patch.object(sys,'argv',['fetch.py','--save',str(saved)]):
                self.assertEqual(fetch.main(),1)
                build.assert_not_called()
            self.assertFalse(saved.exists())
            for path in protected:
                self.assertEqual(path.read_bytes(),b'previous good build')


if __name__=='__main__':
    unittest.main()
