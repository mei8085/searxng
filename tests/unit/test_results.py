# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,disable=missing-class-docstring,invalid-name


from urllib.parse import urlparse

from searx.result_types import LegacyResult, MainResult, File
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
        self.assertSameIdentity("https://example.org/p?q=a%20b", "https://example.org/p?q=a+b")

    def test_query_bare_name_and_blank_value(self):
        # a bare name, a blank value and a missing parameter are three
        # different things and must not collapse into one another
        self.assertSameIdentity("http://h/?a&b=1", "http://h/?b=1&a")
        self.assertDifferentIdentity("http://h/?a", "http://h/?a=")
        self.assertDifferentIdentity("http://h/?a", "http://h/")
        self.assertDifferentIdentity("http://h/?a=", "http://h/")

    def test_repeated_parameters_order_is_significant(self):
        self.assertDifferentIdentity("http://h/?tag=x&tag=y", "http://h/?tag=y&tag=x")
        # the same values repeated in the same order are still equivalent
        self.assertSameIdentity("http://h/?tag=x&tag=y&a=1", "http://h/?a=1&tag=x&tag=y")

    def test_query_value_encoding(self):
        # form-encoded space ('+' / '%20') spellings are equivalent, a real
        # plus sign ('%2B') is different content
        self.assertDifferentIdentity("http://h/?q=a+b", "http://h/?q=a%2Bb")
        self.assertSameIdentity("http://h/?q=%41", "http://h/?q=A")
        self.assertSameIdentity("http://h/?q=%C3%A9", "http://h/?q=%c3%a9")
        # different percent-encoded bytes are different content
        self.assertDifferentIdentity("http://h/?q=%ff", "http://h/?q=%fe")
        # encodings of reserved characters keep their boundary
        self.assertDifferentIdentity("http://h/?q=a/b", "http://h/?q=a%2Fb")
        self.assertDifferentIdentity("http://h/?q=a;", "http://h/?q=a%3B")
        # raw non-ASCII characters are compared by their UTF-8 bytes
        self.assertSameIdentity("http://h/?q=é", "http://h/?q=%C3%A9")

    def test_tracking_parameters_ignored(self):
        self.assertSameIdentity(
            "https://example.org/p?a=1&utm_source=newsletter", "https://example.org/p?a=1"
        )
        self.assertSameIdentity(
            "https://example.org/p?fbclid=abc&gclid=xyz&id=7", "https://example.org/p?id=7"
        )

    def test_tracking_only_query_equals_no_query(self):
        # a page URL that only carries tracking parameters is the same page
        # as the one without any query string
        self.assertSameIdentity("https://example.org/p?utm_source=x", "https://example.org/p")
        self.assertSameIdentity("https://example.org/p?gclid=A", "https://example.org/p?gclid=B")
        self.assertSameIdentity(
            "https://example.org/p?id=7&gbraid=1&twclid=2", "https://example.org/p?id=7"
        )

    def test_tracking_parameter_match_is_case_insensitive(self):
        self.assertSameIdentity("https://example.org/p?id=7&UTM_SOURCE=x", "https://example.org/p?id=7")

    def test_semicolon_is_a_parameter_separator(self):
        self.assertSameIdentity(
            "https://example.org/p?id=7;utm_source=x", "https://example.org/p?id=7"
        )
        # but content separated by ";" is still content
        self.assertDifferentIdentity(
            "https://example.org/p?id=7", "https://example.org/p?id=8;utm_source=x"
        )

    def test_generic_ref_parameters_are_significant(self):
        # "ref" / "referrer" are generic names that carry content on some
        # sites; different values must not be merged away
        self.assertDifferentIdentity("https://example.org/p?ref=homepage", "https://example.org/p")
        self.assertDifferentIdentity("https://example.org/p?ref=a", "https://example.org/p?ref=b")
        self.assertDifferentIdentity("https://example.org/p?referrer=x", "https://example.org/p")
        # unambiguous vendor-specific tracking tokens are still ignored
        self.assertSameIdentity("https://example.org/p?spm=a.b.c", "https://example.org/p")

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

    def test_no_merge_different_query_values(self):
        self._assert_not_merged("https://example.org/p?a=1", "https://example.org/p?a=2")

    def test_no_merge_repeated_parameter_order(self):
        self._assert_not_merged(
            "https://example.org/p?tag=x&tag=y", "https://example.org/p?tag=y&tag=x"
        )

    def test_no_merge_bare_name_and_blank_value(self):
        self._assert_not_merged("https://example.org/p?a", "https://example.org/p?a=")

    def test_no_merge_plus_and_percent_2b(self):
        self._assert_not_merged("https://example.org/p?q=a+b", "https://example.org/p?q=a%2Bb")

    def test_no_merge_different_generic_ref_values(self):
        self._assert_not_merged("https://example.org/p?ref=a", "https://example.org/p?ref=b")
        self._assert_not_merged("https://example.org/p?ref=home", "https://example.org/p")

    def test_merge_tracking_only_differences(self):
        res = self._assert_merged(
            "https://example.org/p?id=7",
            "https://example.org/p?id=7&UTM_SOURCE=news&gbraid=123",
        )
        self.assertEqual(set(res.engines), {"google", "duckduckgo"})

    def test_merge_tracking_only_query_against_no_query(self):
        self._assert_merged("https://example.org/page", "https://example.org/page?gclid=abc")

    def test_merge_semicolon_separated_tracking_parameter(self):
        self._assert_merged(
            "https://example.org/p?id=7",
            "https://example.org/p?id=7;utm_source=newsletter",
        )

    def test_non_default_template_keeps_raw_url_identity(self):
        # file results only merge on literally identical URL spellings; a
        # different query parameter order must stay two results
        container = ResultContainer()
        container.extend(
            "google",
            [File(url="https://example.org/f.pdf?b=2&a=1", title="f", filename="f.pdf")],
        )
        container.extend(
            "duckduckgo",
            [File(url="https://example.org/f.pdf?a=1&b=2", title="f", filename="f.pdf")],
        )
        container.close()
        self.assertEqual(len(container.get_ordered_results()), 2)

    def test_non_default_legacy_template_keeps_raw_url_identity(self):
        container = ResultContainer()
        container.extend(
            "google",
            [dict(template="torrent.html", url="https://example.org/t?b=2&a=1", engine="google")],
        )
        container.extend(
            "duckduckgo",
            [dict(template="torrent.html", url="https://example.org/t?a=1&b=2", engine="duckduckgo")],
        )
        container.close()
        self.assertEqual(len(container.get_ordered_results()), 2)

    def test_non_default_template_still_merges_identical_url(self):
        # exactly identical raw URLs still merge, and the original HTTPS URL
        # is preferred over HTTP
        container = ResultContainer()
        container.extend(
            "google",
            [File(url="http://example.org/f.pdf", title="f", filename="f.pdf")],
        )
        container.extend(
            "duckduckgo",
            [File(url="https://example.org/f.pdf", title="f", filename="f.pdf")],
        )
        container.close()
        result_list = container.get_ordered_results()
        self.assertEqual(len(result_list), 1)
        self.assertEqual(result_list[0].url, "https://example.org/f.pdf")
        self.assertEqual(set(result_list[0].engines), {"google", "duckduckgo"})

    def test_file_merge_prefers_ftps_scheme(self):
        # legacy behavior: on an ftp/ftps match the secure scheme wins by
        # upgrading the scheme of the original (ftp) URL
        container = ResultContainer()
        container.extend(
            "google",
            [File(url="ftp://example.org/pub/f.tar.gz", title="f", filename="f.tar.gz")],
        )
        container.extend(
            "duckduckgo",
            [File(url="ftps://example.org/pub/f.tar.gz", title="f", filename="f.tar.gz")],
        )
        container.close()
        result_list = container.get_ordered_results()
        self.assertEqual(len(result_list), 1)
        self.assertEqual(result_list[0].url, "ftps://example.org/pub/f.tar.gz")
        self.assertEqual(result_list[0].parsed_url.scheme, "ftps")

    def test_legacy_file_merge_prefers_ftps_scheme(self):
        container = ResultContainer()
        container.extend(
            "google",
            [dict(template="torrent.html", url="ftp://example.org/f.torrent", engine="google")],
        )
        container.extend(
            "duckduckgo",
            [dict(template="torrent.html", url="ftps://example.org/f.torrent", engine="duckduckgo")],
        )
        container.close()
        result_list = container.get_ordered_results()
        self.assertEqual(len(result_list), 1)
        self.assertEqual(result_list[0].url, "ftps://example.org/f.torrent")

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
