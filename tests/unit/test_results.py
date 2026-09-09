# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,disable=missing-class-docstring,invalid-name


from urllib.parse import urlparse

from searx.result_types import LegacyResult, MainResult
from searx.result_types._base import _url_identity_key
from searx.results import ResultContainer
from tests import SearxTestCase


def _key(url):
    return _url_identity_key(urlparse(url))


class URLIdentityKeyTestCase(SearxTestCase):
    """Equivalent spellings of the same URL must share one identity key."""

    def assertSameIdentity(self, url_a, url_b):  # pylint: disable=invalid-name
        self.assertEqual(_key(url_a), _key(url_b), f"{url_a!r} != {url_b!r}")

    def assertDifferentIdentity(self, url_a, url_b):  # pylint: disable=invalid-name
        self.assertNotEqual(_key(url_a), _key(url_b), f"{url_a!r} == {url_b!r}")

    def test_scheme_http_https(self):
        self.assertSameIdentity("http://example.org/a", "https://example.org/a")
        self.assertSameIdentity("HTTP://EXAMPLE.ORG/a", "https://example.org/a")

    def test_other_schemes_not_merged(self):
        self.assertDifferentIdentity("ftp://example.org/a", "http://example.org/a")
        self.assertDifferentIdentity("https://example.org/a", "ftps://example.org/a")

    def test_hostname_spelling(self):
        self.assertSameIdentity("http://Example.ORG/a", "http://example.org/a")
        self.assertSameIdentity("http://example.org./a", "http://example.org/a")
        self.assertSameIdentity("http://example.com./a", "http://EXAMPLE.com/a")

    def test_www_prefix_is_significant(self):
        # www. and the apex are *not* merged because some hosts serve different
        # resources there
        self.assertDifferentIdentity("https://example.org/a", "https://www.example.org/a")

    def test_default_ports(self):
        self.assertSameIdentity("http://example.org:80/a", "http://example.org/a")
        self.assertSameIdentity("https://example.org:443/a", "https://example.org/a")

    def test_non_default_ports_are_significant(self):
        self.assertDifferentIdentity("http://example.org:8080/a", "http://example.org/a")
        self.assertDifferentIdentity("https://example.org:8443/a", "https://example.org/a")

    def test_path_dot_segments(self):
        self.assertSameIdentity("http://example.org/a/./b/../c", "http://example.org/a/c")
        self.assertSameIdentity("http://example.org/a/b/c/./../../g", "http://example.org/a/g")
        self.assertDifferentIdentity("http://example.org/a/../b", "http://example.org/b/c")

    def test_path_percent_encoding(self):
        self.assertSameIdentity("http://example.org/a%7eb", "http://example.org/a~b")
        self.assertSameIdentity("http://example.org/a%2fb", "http://example.org/a%2Fb")
        # an encoded slash must not be decoded (it would change the path)
        self.assertDifferentIdentity("http://example.org/a%2Fb", "http://example.org/a/b")

    def test_empty_path_equals_slash(self):
        self.assertSameIdentity("https://example.org", "https://example.org/")
        # but a trailing slash on a non-empty path stays significant
        self.assertDifferentIdentity("https://example.org/a", "https://example.org/a/")

    def test_query_parameter_order(self):
        self.assertSameIdentity("https://example.org/p?b=2&a=1", "https://example.org/p?a=1&b=2")
        self.assertSameIdentity("https://example.org/p?q=a+b", "https://example.org/p?q=a%20b")

    def test_tracking_parameters_ignored(self):
        self.assertSameIdentity(
            "https://example.org/p?a=1&utm_source=newsletter", "https://example.org/p?a=1"
        )
        self.assertSameIdentity(
            "https://example.org/p?fbclid=abc&gclid=xyz&id=7", "https://example.org/p?id=7"
        )

    def test_meaningful_query_parameters_are_significant(self):
        self.assertDifferentIdentity("https://example.org/p?a=1", "https://example.org/p?a=2")
        self.assertDifferentIdentity("https://example.org/p?a=1", "https://example.org/p?b=1")
        # removing a non-tracking parameter changes the resource
        self.assertDifferentIdentity("https://example.org/p?a=1&b=2", "https://example.org/p?a=1")

    def test_fragment(self):
        self.assertSameIdentity("https://example.org/p#x%2fy", "https://example.org/p#x%2Fy")
        self.assertDifferentIdentity("https://example.org/p#a", "https://example.org/p#b")
        self.assertDifferentIdentity("https://example.org/p", "https://example.org/p#frag")

    def test_userinfo_is_significant(self):
        self.assertDifferentIdentity("http://user@example.org/a", "http://example.org/a")

    def test_display_url_is_not_mutated(self):
        parsed = urlparse("http://example.org:80/a/./b?b=2&a=1&utm_source=x#Top")
        before = parsed.geturl()
        _ = _url_identity_key(parsed)
        self.assertEqual(parsed.geturl(), before)


class ResultContainerTestCase(SearxTestCase):
    # pylint: disable=use-dict-literal

    TEST_SETTINGS = "test_result_container.yml"

    def test_empty(self):
        container = ResultContainer()
        self.assertEqual(container.get_ordered_results(), [])

    def test_one_result(self):
        result = dict(url="https://example.org", title="title ..", content="Lorem ..")

        container = ResultContainer()
        container.extend("google", [result])
        container.close()

        self.assertEqual(len(container.get_ordered_results()), 1)

        res = LegacyResult(result)
        res.normalize_result_fields()
        self.assertIn(res, container.get_ordered_results())

    def test_one_suggestion(self):
        result = dict(suggestion="lorem ipsum ..")

        container = ResultContainer()
        container.extend("duckduckgo", [result])
        container.close()

        self.assertEqual(len(container.get_ordered_results()), 0)
        self.assertEqual(len(container.suggestions), 1)
        self.assertIn(result["suggestion"], container.suggestions)

    def test_merge_url_result(self):
        # from the merge of eng1 and eng2 we expect this result
        result = LegacyResult(
            url="https://example.org", title="very long title, lorem ipsum", content="Lorem ipsum dolor sit amet .."
        )
        result.normalize_result_fields()
        eng1 = dict(url=result.url, title="short title", content=result.content, engine="google")
        eng2 = dict(url="http://example.org", title=result.title, content="lorem ipsum", engine="duckduckgo")

        container = ResultContainer()
        container.extend(None, [eng1, eng2])
        container.close()

        result_list = container.get_ordered_results()
        self.assertEqual(len(container.get_ordered_results()), 1)
        self.assertIn(result, result_list)
        self.assertEqual(result_list[0].title, result.title)
        self.assertEqual(result_list[0].content, result.content)

    def _assert_merged(self, url_a, url_b, engine_a="google", engine_b="duckduckgo", typed=False):
        container = ResultContainer()
        if typed:
            container.extend(engine_a, [MainResult(url=url_a, title="title", content="content")])
            container.extend(engine_b, [MainResult(url=url_b, title="title", content="content")])
        else:
            container.extend(
                engine_a,
                [dict(url=url_a, title="title", content="content", engine=engine_a)],
            )
            container.extend(
                engine_b,
                [dict(url=url_b, title="title", content="content", engine=engine_b)],
            )
        container.close()
        result_list = container.get_ordered_results()
        self.assertEqual(len(result_list), 1, f"{url_a} and {url_b} not merged")
        return result_list[0]

    def _assert_not_merged(self, url_a, url_b):
        container = ResultContainer()
        container.extend("google", [dict(url=url_a, title="title", content="content", engine="google")])
        container.extend(
            "duckduckgo", [dict(url=url_b, title="title", content="content", engine="duckduckgo")]
        )
        container.close()
        self.assertEqual(len(container.get_ordered_results()), 2, f"{url_a} and {url_b} wrongly merged")

    def test_merge_query_spelling_legacies(self):
        res = self._assert_merged(
            "https://example.org/p?b=2&a=1",
            "https://example.org/p?a=1&b=2&utm_source=newsletter",
        )
        self.assertEqual(set(res.engines), {"google", "duckduckgo"})
        self.assertEqual(len(res.positions), 2)
        self.assertGreater(res.score, 0)

    def test_merge_host_port_path_spelling(self):
        self._assert_merged(
            "http://Example.ORG:80/a/./b/../c?x=1",
            "https://example.org/a/c?x=1",
        )

    def test_merge_empty_path_and_slash(self):
        self._assert_merged("https://example.org", "https://example.org/")

    def test_merge_typed_main_results(self):
        res = self._assert_merged(
            "http://example.org/a?b=2&a=1",
            "https://example.org/a?a=1&b=2",
            typed=True,
        )
        self.assertIsInstance(res, MainResult)
        self.assertEqual(set(res.engines), {"google", "duckduckgo"})

    def test_no_merge_different_ports(self):
        self._assert_not_merged("http://example.org:8080/a", "http://example.org/a")

    def test_no_merge_different_paths(self):
        self._assert_not_merged("https://example.org/a", "https://example.org/a/")

    def test_no_merge_different_fragments(self):
        self._assert_not_merged("https://example.org/a#one", "https://example.org/a#two")

    def test_no_merge_different_hosts(self):
        self._assert_not_merged("https://example.org/a", "https://www.example.org/a")

    def test_merge_prefers_original_https_url(self):
        # the engine-returned HTTPS URL is kept verbatim (here even with its
        # tracking parameter), instead of swapping the scheme of the http URL
        https_url = "https://example.org/a?utm_source=newsletter&b=2"
        res = self._assert_merged("http://example.org/a?b=2", https_url)
        self.assertEqual(res.url, https_url)
        self.assertEqual(res.parsed_url.scheme, "https")

    def test_merge_keeps_https_when_already_https(self):
        https_url = "https://example.org/a?b=2&a=1"
        res = self._assert_merged(https_url, "http://example.org/a?a=1&b=2")
        self.assertEqual(res.url, https_url)

    def test_merge_preserves_title_and_content_selection(self):
        container = ResultContainer()
        container.extend(
            "google",
            [dict(url="http://example.org/p?a=1", title="short", content="short content", engine="google")],
        )
        container.extend(
            "duckduckgo",
            [
                dict(
                    url="https://example.org/p?a=1&utm_campaign=x",
                    title="a much longer title",
                    content="a clearly longer content text",
                    engine="duckduckgo",
                )
            ],
        )
        container.close()
        res = container.get_ordered_results()[0]
        self.assertEqual(res.title, "a much longer title")
        self.assertEqual(res.content, "a clearly longer content text")
        self.assertEqual(res.url, "https://example.org/p?a=1&utm_campaign=x")
