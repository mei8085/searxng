# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=too-few-public-methods, missing-module-docstring
"""Basic types for the typification of results.

- :py:obj:`Result` base class
- :py:obj:`LegacyResult` for internal use only

----

.. autoclass:: Result
   :members:

.. _LegacyResult:

.. autoclass:: LegacyResult
   :members:
"""

__all__ = ["Result"]

import typing as t

import re
import urllib.parse
import warnings
import datetime

from collections.abc import Callable

import msgspec

from searx import logger as log

WHITESPACE_REGEX = re.compile('( |\t|\n)+', re.M | re.U)
UNSET = object()

# template of ordinary web page results; only these use the canonical URL
# identity (_url_identity_key) when detecting duplicates.  Other result
# types (files, papers, torrents, maps, ..) keep their template-specific
# / raw-URL based identity.
DEFAULT_RESULT_TEMPLATE = "default.html"

# Query parameters that do not identify the resource and are commonly added by
# analytics / link-tracking systems.  Only parameters whose meaning is
# unambiguous are listed here in order not to merge different resources.
TRACKING_QUERY_PARAMS = frozenset(
    [
        # Google Analytics / generic Urchin Tracking Module
        "utm_id",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_cid",
        "utm_name",
        "utm_referrer",
        "utm_social",
        "utm_social-type",
        # Matomo
        "mtm_cid",
        "mtm_keyword",
        "mtm_source",
        "mtm_medium",
        "mtm_campaign",
        "mtm_content",
        "mtm_group",
        "mtm_placement",
        "pk_campaign",
        "pk_kwd",
        "pk_keyword",
        # ad / social click identifiers
        "gclid",
        "gclsrc",
        "dclid",
        "fbclid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "vero_conv",
        "vero_id",
        "ref",
        "referrer",
        "spm",
    ]
)

# default ports that do not need to be part of the identity of an URL
_DEFAULT_PORTS: dict[str, int] = {
    "http": 80,
    "https": 443,
    "ftp": 21,
    "ws": 80,
    "wss": 443,
}

# percent-encodings of characters that RFC 3986 classifies as "unreserved"
# (ALPHA / DIGIT / "-" / "." / "_" / "~") and therefore never need encoding
_PERCENT_UNRESERVED: dict[str, str] = {
    f"{i:02x}": c
    for i in range(256)
    for c in (chr(i),)
    if c.isascii() and (c.isalnum() or c in "-._~")
}
_PERCENT_UNRESERVED.update({k.upper(): v for k, v in _PERCENT_UNRESERVED.items()})


def _is_tracking_query_param(name: str) -> bool:
    return name in TRACKING_QUERY_PARAMS or name.startswith("utm_")


def _normalize_percent_encoding(value: str) -> str:
    """Upper-case percent escapes and decode percent-encodings of unreserved
    characters (RFC 3986, section 2.3).  Any other encoding is left untouched,
    even when it looks redundant, because decoding a reserved character can
    change the semantics of the URL."""

    if "%" not in value:
        return value
    parts = value.split("%")
    out = [parts[0]]
    for part in parts[1:]:
        if len(part) >= 2:
            decoded = _PERCENT_UNRESERVED.get(part[:2])
            if decoded is not None:
                out.append(decoded + part[2:])
            else:
                # upper-case the hex digits, keep the encoded byte
                out.append("%" + part[:2].upper() + part[2:])
        else:
            out.append("%" + part)
    return "".join(out)


def _remove_dot_segments(path: str) -> str:
    """RFC 3986, section 5.2.4 remove_dot_segments algorithm."""

    inp = path
    out: list[str] = []
    while inp:
        if inp.startswith("../"):
            inp = inp[3:]
        elif inp.startswith("./"):
            inp = inp[2:]
        elif inp.startswith("/./"):
            inp = "/" + inp[3:]
        elif inp == "/.":
            inp = "/"
        elif inp.startswith("/../"):
            inp = "/" + inp[4:]
            if out:
                out.pop()
        elif inp == "/..":
            inp = "/"
            if out:
                out.pop()
        elif inp in (".", ".."):
            inp = ""
        else:
            # move the first path segment (including a leading slash) to out
            if inp.startswith("/"):
                idx = inp.find("/", 1)
                seg, inp = (inp, "") if idx == -1 else (inp[:idx], inp[idx:])
            else:
                idx = inp.find("/")
                seg, inp = (inp, "") if idx == -1 else (inp[:idx], inp[idx:])
            out.append(seg)
    return "".join(out)


def _normalize_query_component(value: str) -> str:
    """Normalize the spelling of one query parameter name or value.

    In a form-urlencoded query a literal ``+`` means a space (as does a
    raw space / ``%20``), so these three spellings collapse to the same
    value; the percent-encoded ``%2B`` (a real plus sign) stays distinct.
    Apart from that, the same conservative rule as for paths applies:
    percent-encodings of unreserved characters are decoded, hex digits are
    upper-cased, but percent-encodings of reserved characters keep their
    encoding boundary (``a/b`` and ``a%2Fb`` are not the same query).
    Raw non-ASCII characters are UTF-8 percent-encoded.
    """

    value = value.replace("+", "%20")
    result = _normalize_percent_encoding(value)
    return "".join(
        ch if ord(ch) < 128 else "".join(f"%{b:02X}" for b in ch.encode("utf-8"))
        for ch in result
    ).replace(" ", "%20")


def _query_identity(query: str) -> tuple:
    """Build an identity value for a query string that distinguishes real
    content differences while ignoring purely cosmetic spelling:

    - parameters may appear in any order *as long as every parameter name
      occurs at most once*;
    - if a parameter name occurs more than once, the values keep their
      original order for that name (``tag=x&tag=y`` and ``tag=y&tag=x``
      describe a different order of content and are not merged);
    - a bare parameter name (``a``) is different from an empty value
      (``a=``);
    - equivalent percent-encodings of the same bytes are merged.
    """

    if query == "":
        return ()

    single: dict[str, str | None] = {}
    repeated: dict[str, list[str | None]] = {}
    for piece in query.split("&"):
        if piece == "":
            # a trailing/duplicate "&" carries no content
            continue
        if "=" in piece:
            name, value = piece.split("=", 1)
            value_key: str | None = _normalize_query_component(value)
        else:
            name, value_key = piece, None
        name_key = _normalize_query_component(name)
        if _is_tracking_query_param(name_key):
            continue
        if name_key in single:
            bucket = repeated.setdefault(name_key, [single.pop(name_key)])
            bucket.append(value_key)
        elif name_key in repeated:
            repeated[name_key].append(value_key)
        else:
            single[name_key] = value_key

    single_items = tuple(sorted(single.items()))
    repeated_items = tuple(sorted((name, tuple(values)) for name, values in repeated.items()))
    return single_items, repeated_items


def _url_identity_key(parsed_url: urllib.parse.ParseResult) -> tuple:
    """Build a conservative identity key for an URL.

    Results whose URL differs only in the normalized spelling are considered
    duplicates.  The normalization is deliberately conservative to avoid
    merging different resources:

    - ``http`` and ``https`` share one identity (the merge prefers the
      original HTTPS URL for display), other schemes are compared literally;
    - host names are lower-cased, a trailing dot is stripped and IDN names
      are compared by their ASCII (IDNA) form;
    - the default port of a scheme is ignored;
    - percent-encoding is harmonized (upper-case hex, unreserved characters
      decoded) and ``.`` / ``..`` path segments are resolved;
    - single-occurrence query parameters are order-insensitive; the order of
      values of a repeated parameter name is preserved (see
      :py:func:`_query_identity`), equivalent percent-encodings are merged
      and well-known tracking parameters (``utm_*`` and click identifiers)
      are ignored;
    - a literal ``+`` is never treated as an encoded space, so ``a+b`` and
      ``a%2Bb`` keep different query values.

    Values that are not safe to normalize across the board (``www.``
    prefixes, trailing slashes on non-empty paths, fragments) are kept
    as-is.  The function never modifies the passed URL.
    """

    scheme = (parsed_url.scheme or "http").lower()
    try:
        hostname = parsed_url.hostname or ""
        port = parsed_url.port
    except ValueError:
        # malformed netloc (e.g. invalid port): fall back to the raw values
        # and only compare the scheme case-insensitively
        return (
            "https" if scheme in ("http", "https") else scheme,
            parsed_url.netloc,
            parsed_url.path,
            parsed_url.params,
            parsed_url.query,
            parsed_url.fragment,
        )

    hostname = hostname.rstrip(".")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        pass  # keep the (already ASCII) host name if IDNA encoding fails

    # userinfo (user:pass@) is part of the resource authority
    userinfo = ""
    netloc = parsed_url.netloc
    if "@" in netloc:
        userinfo = netloc.rsplit("@", 1)[0]

    netloc_key = hostname
    if userinfo:
        netloc_key = f"{userinfo}@{netloc_key}"
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc_key = f"{netloc_key}:{port}"

    path = _normalize_percent_encoding(_remove_dot_segments(parsed_url.path))
    if parsed_url.netloc and path == "":
        # RFC 3986: an empty path is equivalent to "/" in an URI with authority
        path = "/"

    query = _query_identity(parsed_url.query)

    fragment = _normalize_percent_encoding(parsed_url.fragment)

    return (
        "https" if scheme in ("http", "https") else scheme,
        netloc_key,
        path,
        parsed_url.params,
        query,
        fragment,
    )


def _raw_url_identity_hash(template: str, parsed_url: urllib.parse.ParseResult, img_src: str) -> int:
    """Identity of a non-ordinary result type (file, paper, torrent, map, ..).

    Historically results were duplicates when their values for template,
    the raw ``parsed_url`` components (except the scheme) and ``img_src``
    were literally equal.  This spelling-sensitive behavior is kept for
    every non-default template so result-type specific dedup is unchanged.
    """

    return hash(
        f"{template}"
        + f"|{parsed_url.netloc}|{parsed_url.path}|{parsed_url.params}|{parsed_url.query}|{parsed_url.fragment}"
        + f"|{img_src}"
    )


def _normalize_url_fields(result: "Result | LegacyResult"):

    # As soon we need LegacyResult not any longer, we can move this function to
    # method Result.normalize_result_fields

    if result.url and not result.parsed_url:
        if not isinstance(result.url, str):
            log.debug('result: invalid URL: %s', str(result))
            result.url = ""
            result.parsed_url = None
        else:
            result.parsed_url = urllib.parse.urlparse(result.url)

    if result.parsed_url:
        result.parsed_url = result.parsed_url._replace(
            # if the result has no scheme, use http as default
            scheme=result.parsed_url.scheme or "http",
            path=result.parsed_url.path,
        )
        result.url = result.parsed_url.geturl()

    if isinstance(result, LegacyResult) and getattr(result, "infobox", None):
        # As soon we have InfoboxResult, we can move this function to method
        # InfoboxResult.normalize_result_fields

        infobox_urls: list[dict[str, str]] = getattr(result, "urls", [])
        for item in infobox_urls:
            _url = item.get("url")
            if not _url:
                continue
            _url = urllib.parse.urlparse(_url)
            item["url"] = _url._replace(
                scheme=_url.scheme or "http",
                # netloc=_url.netloc.replace("www.", ""),
                path=_url.path,
            ).geturl()

        infobox_id: str | None = getattr(result, "id", None)
        if infobox_id:
            _url = urllib.parse.urlparse(infobox_id)
            result.id = _url._replace(
                scheme=_url.scheme or "http",
                # netloc=_url.netloc.replace("www.", ""),
                path=_url.path,
            ).geturl()


def _normalize_text_fields(result: "MainResult | LegacyResult"):

    # As soon we need LegacyResult not any longer, we can move this function to
    # method MainResult.normalize_result_fields

    # Actually, a type check should not be necessary if the engine is
    # implemented correctly. Historically, however, we have always had a type
    # check here.

    if result.title and not isinstance(result.title, str):
        log.debug("result: invalid type of field 'title': %s", str(result))
        result.title = str(result)
    if result.content and not isinstance(result.content, str):
        log.debug("result: invalid type of field 'content': %s", str(result))
        result.content = str(result)

    # normalize title and content
    if result.title:
        result.title = WHITESPACE_REGEX.sub(" ", result.title).strip()
    if result.content:
        result.content = WHITESPACE_REGEX.sub(" ", result.content).strip()
    if result.content == result.title:
        # avoid duplicate content between the content and title fields
        result.content = ""


def _filter_urls(
    result: "Result | LegacyResult", filter_func: "Callable[[Result | LegacyResult, str, str], str | bool]"
):
    # pylint: disable=too-many-branches, too-many-statements

    # As soon we need LegacyResult not any longer, we can move this function to
    # method Result.

    url_fields = ["url", "iframe_src", "audio_src", "img_src", "thumbnail_src", "thumbnail"]

    url_src: str

    for field_name in url_fields:
        url_src = getattr(result, field_name, "")
        if not url_src:
            continue

        new_url = filter_func(result, field_name, url_src)
        # log.debug("filter_urls: filter_func(result, %s) '%s' -> '%s'", field_name, field_value, new_url)
        if isinstance(new_url, bool):
            if new_url:
                # log.debug("filter_urls: unchanged field %s URL %s", field_name, field_value)
                continue
            log.debug("filter_urls: drop field %s URL %s", field_name, url_src)
            new_url = None
        else:
            log.debug("filter_urls: modify field %s URL %s -> %s", field_name, url_src, new_url)

        setattr(result, field_name, new_url)
        if field_name == "url":
            # sync parsed_url with new_url
            if not new_url:
                result.parsed_url = None
            elif isinstance(new_url, str):
                result.parsed_url = urllib.parse.urlparse(new_url)

    # "urls": are from infobox
    #
    # As soon we have InfoboxResult, we can move this function to method
    # InfoboxResult.normalize_result_fields

    infobox_urls: list[dict[str, str]] = getattr(result, "urls", [])

    if infobox_urls:
        # log.debug("filter_urls: infobox_urls .. %s", infobox_urls)
        new_infobox_urls: list[dict[str, str]] = []

        for item in infobox_urls:
            url_src = item.get("url", "")
            if not url_src:
                new_infobox_urls.append(item)
                continue

            new_url = filter_func(result, "infobox_urls", url_src)
            if isinstance(new_url, bool):
                if new_url:
                    new_infobox_urls.append(item)
                    # log.debug("filter_urls: leave URL in field 'urls' ('infobox_urls') unchanged -> %s", _url)
                    continue
                log.debug("filter_urls: remove URL from field 'urls' ('infobox_urls') URL %s", url_src)
                new_url = None
            if new_url:
                log.debug("filter_urls: modify URL from field 'urls' ('infobox_urls') URL %s -> %s", url_src, new_url)
                item["url"] = new_url
                new_infobox_urls.append(item)

        setattr(result, "urls", new_infobox_urls)

    # "attributes": are from infobox
    #
    # The infobox has additional subsections for attributes, urls and relatedTopics:

    infobox_attributes: list[dict[str, t.Any]] = getattr(result, "attributes", [])

    if infobox_attributes:
        # log.debug("filter_urls: infobox_attributes .. %s", infobox_attributes)
        new_infobox_attributes: list[dict[str, str | list[dict[str, str]]]] = []

        for item in infobox_attributes:
            image: dict[str, str] = item.get("image", {})
            url_src = image.get("src", "")
            if not url_src:
                new_infobox_attributes.append(item)
                continue

            new_url = filter_func(result, "infobox_attributes", url_src)
            if isinstance(new_url, bool):
                if new_url:
                    new_infobox_attributes.append(item)
                    # log.debug("filter_urls: leave URL in field 'image.src' unchanged -> %s", url_src)
                    continue
                log.debug("filter_urls: drop field 'image.src' ('infobox_attributes') URL %s", url_src)
                new_url = None

            if new_url:
                log.debug(
                    "filter_urls: modify 'image.src' ('infobox_attributes') URL %s -> %s",
                    url_src,
                    new_url,
                )
                item["image"]["src"] = new_url
                new_infobox_attributes.append(item)

        setattr(result, "attributes", new_infobox_attributes)

    result.normalize_result_fields()


def _normalize_date_fields(result: "MainResult | LegacyResult"):

    if result.publishedDate:  # do not try to get a date from an empty string or a None type
        try:  # test if publishedDate >= 1900 (datetime module bug)
            result.pubdate = result.publishedDate.strftime('%Y-%m-%d %H:%M:%S%z')
        except ValueError:
            result.publishedDate = None


class Result(msgspec.Struct, kw_only=True):
    """Base class of all result types :ref:`result types`."""

    url: str | None = None
    """A link related to this *result*"""

    engine: str | None = ""
    """Name of the engine *this* result comes from.  In case of *plugins* a
    prefix ``plugin:`` is set, in case of *answerer* prefix ``answerer:`` is
    set.

    The field is optional and is initialized from the context if necessary.
    """

    parsed_url: urllib.parse.ParseResult | None = None
    """:py:obj:`urllib.parse.ParseResult` of :py:obj:`Result.url`.

    The field is optional and is initialized from the context if necessary.
    """

    def normalize_result_fields(self):
        """Normalize fields ``url`` and ``parse_sql``.

        - If field ``url`` is set and field ``parse_url`` is unset, init
          ``parse_url`` from field ``url``.  The ``url`` field is initialized
          with the resulting value in ``parse_url``, if ``url`` and
          ``parse_url`` are not equal.
        """
        _normalize_url_fields(self)

    def __post_init__(self):
        pass

    def filter_urls(self, filter_func: "Callable[[Result | LegacyResult, str, str], str | bool]"):
        """A filter function is passed in the ``filter_func`` argument to
        filter and/or modify the URLs.

        The filter function receives the :py:obj:`result object <Result>` as
        the first argument and the field name (``str``) in the second argument.
        In the third argument the URL string value is passed to the filter function.

        The filter function is applied to all fields that contain a URL,
        in addition to the familiar ``url`` field, these include fields such as::

             ["url", "iframe_src", "audio_src", "img_src", "thumbnail_src", "thumbnail"]

        and the ``urls`` list of items of the infobox.

        For each field, the filter function is called and returns a bool or a
        string value:

        - ``True``: leave URL in field unchanged
        - ``False``: remove URL field from result (or remove entire result)
        - ``str``: modified URL to be used instead

        See :ref:`filter urls example`.

        """
        _filter_urls(self, filter_func=filter_func)

    def __hash__(self) -> int:
        """Generates a hash value that uniquely identifies the content of *this*
        result.  The method can be adapted in the inheritance to compare results
        from different sources.

        If two result objects are not identical but have the same content, their
        hash values should also be identical.

        The hash value is used in contexts, e.g. when checking for equality to
        identify identical results from different sources (engines).
        """
        return id(self)

    def __eq__(self, other: object):
        """py:obj:`Result` objects are equal if the hash values of the two
        objects are equal.  If needed, its recommended to overwrite
        "py:obj:`Result.__hash__`."""

        return hash(self) == hash(other)

    # for legacy code where a result is treated as a Python dict

    def __setitem__(self, field_name: str, value: t.Any):

        return setattr(self, field_name, value)

    def __getitem__(self, field_name: str) -> t.Any:

        if field_name not in self.__struct_fields__:
            raise KeyError(f"{field_name}")
        return getattr(self, field_name)

    def __iter__(self):

        return iter(self.__struct_fields__)

    def as_dict(self):
        return {f: getattr(self, f) for f in self.__struct_fields__}

    def defaults_from(self, other: "Result"):
        """Fields not set in *self* will be updated from the field values of the
        *other*.  If a field is set (exists) but contains an empty string
        or the value ``None``, it is also considered *not set*.
        """
        for field_name in self.__struct_fields__:
            self_val = getattr(self, field_name, UNSET)
            other_val = getattr(other, field_name, UNSET)
            if self_val is UNSET and other_val not in (UNSET, "", None):
                setattr(self, field_name, other_val)


class MainResult(Result):  # pylint: disable=missing-class-docstring
    """Base class of all result types displayed in :ref:`area main results`."""

    template: str = "default.html"
    """Name of the template used to render the result.

    By default :origin:`result_templates/default.html
    <searx/templates/simple/result_templates/default.html>` is used.
    """

    title: str = ""
    """Link title of the result item."""

    content: str = ""
    """Extract or description of the result item"""

    img_src: str = ""
    """URL of a image that is displayed in the result item."""

    iframe_src: str = ""
    """URL of an embedded ``<iframe>`` / the frame is collapsible."""

    audio_src: str = ""
    """URL of an embedded ``<audio controls>``."""

    thumbnail: str = ""
    """URL of a thumbnail that is displayed in the result item."""

    publishedDate: datetime.datetime | None = None
    """The date on which the object was published."""

    pubdate: str = ""
    """String representation of :py:obj:`MainResult.publishedDate`

    Deprecated: it is still partially used in the templates, but will one day be
    completely eliminated.
    """

    length: datetime.timedelta | None = None
    """Playing duration in seconds."""

    views: str = ""
    """View count in humanized number format."""

    author: str = ""
    """Author of the title."""

    metadata: str = ""
    """Miscellaneous metadata."""

    PriorityType = t.Literal["", "high", "low"]  # pyright: ignore[reportUnannotatedClassAttribute]
    priority: "MainResult.PriorityType" = ""
    """The priority can be set via :ref:`hostnames plugin`, for example."""

    engines: set[str] = set()
    """In a merged results list, the names of the engines that found this result
    are listed in this field."""

    # open_group and close_group should not manged in the Result
    # class (we should drop it from here!)
    open_group: bool = False
    close_group: bool = False
    positions: list[int] = []
    score: float = 0
    category: str = ""

    def __hash__(self) -> int:
        """Ordinary web page results (``default.html`` template) are equal if
        their :py:obj:`MainResult.img_src` and their canonical URL identity
        (see :py:func:`_url_identity_key`, built from
        :py:obj:`Result.parsed_url`) are equal.

        Result types with their own template (files, papers, torrents, maps,
        ..) keep the spelling-sensitive raw-URL identity
        (:py:func:`_raw_url_identity_hash`) so their type specific behavior
        is unchanged.
        """
        if not self.parsed_url:
            raise ValueError(f"missing a value in field 'parsed_url': {self}")

        if self.template == DEFAULT_RESULT_TEMPLATE:
            return hash((self.template, _url_identity_key(self.parsed_url), self.img_src))
        return _raw_url_identity_hash(self.template, self.parsed_url, self.img_src)

    def normalize_result_fields(self):
        super().normalize_result_fields()
        _normalize_text_fields(self)
        _normalize_date_fields(self)
        if self.engine:
            self.engines.add(self.engine)


class LegacyResult(dict[str, t.Any]):
    """A wrapper around a legacy result item.  The SearXNG core uses this class
    for untyped dictionaries / to be downward compatible.

    This class is needed until we have implemented an :py:obj:`Result` class for
    each result type and the old usages in the codebase have been fully
    migrated.

    There is only one place where this class is used, in the
    :py:obj:`searx.results.ResultContainer`.

    .. attention::

       Do not use this class in your own implementations!
    """

    # emulate field types from type class Result
    url: str | None
    template: str
    engine: str
    parsed_url: urllib.parse.ParseResult | None

    # emulate field types from type class MainResult
    title: str
    content: str
    img_src: str
    thumbnail: str
    priority: t.Literal["", "high", "low"]
    engines: set[str]
    positions: list[int]
    score: float
    category: str
    publishedDate: datetime.datetime | None
    pubdate: str = ""

    # infobox result
    urls: list[dict[str, str]]
    attributes: list[dict[str, str]]

    def as_dict(self):
        return self

    def __init__(self, *args: t.Any, **kwargs: t.Any):

        super().__init__(*args, **kwargs)

        # emulate field types from type class Result
        self["url"] = self.get("url")
        self["template"] = self.get("template", "default.html")
        self["engine"] = self.get("engine", "")
        self["parsed_url"] = self.get("parsed_url")

        # emulate field types from type class MainResult
        self["title"] = self.get("title", "")
        self["content"] = self.get("content", "")
        self["img_src"] = self.get("img_src", "")
        self["thumbnail"] = self.get("thumbnail", "")
        self["priority"] = self.get("priority", "")
        self["engines"] = self.get("engines", set())
        self["positions"] = self.get("positions", "")
        self["score"] = self.get("score", 0)
        self["category"] = self.get("category", "")
        self["publishedDate"] = self.get("publishedDate")

        if "infobox" in self:
            self["urls"] = self.get("urls", [])
            self["attributes"] = self.get("attributes", [])

        # Legacy types that have already been ported to a type ..

        if "answer" in self:
            warnings.warn(
                f"engine {self.engine} is using deprecated `dict` for answers"
                f" / use a class from searx.result_types.answer",
                DeprecationWarning,
            )
            self.template = "answer/legacy.html"

        if self.template == "keyvalue.html":
            warnings.warn(
                f"engine {self.engine} is using deprecated `dict` for key/value results"
                f" / use a class from searx.result_types",
                DeprecationWarning,
            )

    def __getattr__(self, name: str, default: t.Any = UNSET) -> t.Any:
        if default == UNSET and name not in self:
            raise AttributeError(f"LegacyResult object has no field named: {name}")
        return self[name]

    def __setattr__(self, name: str, val: t.Any):
        self[name] = val

    def __hash__(self) -> int:  # pyright: ignore[reportIncompatibleVariableOverride]

        if "answer" in self:
            # deprecated ..
            return hash(self["answer"])

        if self.template == "images.html":
            # image results are equal if their values for template, the url and
            # the img_src are equal.
            return hash(f"{self.template}|{self.url}|{self.img_src}")

        if not any(cls in self for cls in ["suggestion", "correction", "infobox", "number_of_results", "engine_data"]):
            # Ordinary web page results (default.html template) use the
            # canonical URL identity (`_url_identity_key`, ignoring cosmetic
            # spelling differences); results with their own template (file,
            # paper, torrent, map, ..) keep the spelling-sensitive raw-URL
            # identity (`_raw_url_identity_hash`).

            if not self.parsed_url:
                raise ValueError(f"missing a value in field 'parsed_url': {self}")

            if self.template == DEFAULT_RESULT_TEMPLATE:
                return hash((self.template, _url_identity_key(self.parsed_url), self.img_src))
            return _raw_url_identity_hash(self.template, self.parsed_url, self.img_src)

        return id(self)

    def __eq__(self, other: object):

        return hash(self) == hash(other)

    def __repr__(self) -> str:

        return f"LegacyResult: {super().__repr__()}"

    def normalize_result_fields(self):
        _normalize_date_fields(self)
        _normalize_url_fields(self)
        _normalize_text_fields(self)
        if self.engine:
            self.engines.add(self.engine)

    def defaults_from(self, other: "LegacyResult"):
        # If a field is set (exists) but contains an empty string or the value
        # ``None``, it is also considered *not set*.
        for field_name, other_val in other.items():
            self_val = self.get(field_name, UNSET)
            if self_val is UNSET and other_val not in ("", UNSET):
                self[field_name] = other_val

    def filter_urls(self, filter_func: "Callable[[Result | LegacyResult, str, str], str | bool]"):
        """See :py:obj:`Result.filter_urls`"""
        _filter_urls(self, filter_func=filter_func)
