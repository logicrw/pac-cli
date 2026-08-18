"""Parse BPC extension sites.js into a strategy map."""
import ipaddress
import json
import re
import sys
from functools import lru_cache
from importlib import resources
from pathlib import Path
from dataclasses import dataclass, field, asdict, fields


def _default_sites_js() -> Path:
    """Locate sites.js: PyInstaller bundle → package data → home fallback."""
    if getattr(sys, '_MEIPASS', None):
        return Path(sys._MEIPASS) / "data" / "sites.js"
    packaged_data = Path(__file__).parent / "data" / "sites.js"
    if packaged_data.exists():
        return packaged_data
    repo_data = Path(__file__).parent.parent.parent / "data" / "sites.js"
    if repo_data.exists():
        return repo_data
    return Path.home() / "code/clis/bpc-fetch/data/sites.js"


SITES_JS_DEFAULT = _default_sites_js()


@dataclass
class SiteStrategy:
    domain: str
    name: str = ""
    useragent: str = ""
    useragent_custom: str = ""
    referer: str = ""
    referer_custom: str = ""  # §15 / A1
    random_ip: str = ""
    allow_cookies: bool = False
    block_regex: str = ""
    # Singular fields preserve each upstream rule's source semantics.
    block_regex_general: str = ""
    excluded_domains: list[str] = field(default_factory=list)
    # Effective global blockers applicable to this target domain.
    general_block_regexes: list[str] = field(default_factory=list)
    cs_dompurify: bool = False
    amp: bool = False
    group: list[str] = field(default_factory=list)
    # Unmodeled upstream fields (block_js_inline, remove_cookies_*, cs_code, …) — display only in Phase 1
    extra: dict = field(default_factory=dict)

    def needs_browser_cleanup(self) -> bool:
        """cs_dompurify means DOM cleanup in browser, NOT archive (§S / §15)."""
        return bool(self.cs_dompurify)

    def bypass_type(self) -> str:
        if self.useragent_custom:
            return "ua:custom"
        if self.useragent:
            return f"ua:{self.useragent}"
        if self.referer_custom:
            return "referer:custom"
        if self.referer:
            return f"referer:{self.referer}"
        if self.block_regex:
            return "block_js"
        if self.cs_dompurify:
            return "dom_cleanup"
        return "cookies"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["bypass_type"] = self.bypass_type()
        return d


def strategy_from_dict(v: dict) -> SiteStrategy:
    known = {f.name for f in fields(SiteStrategy)}
    filtered = {k: v[k] for k in v if k in known}
    if "domain" not in filtered:
        filtered["domain"] = v.get("domain", "")
    return SiteStrategy(**filtered)


def parse_sites_js(path: Path | None = None) -> dict[str, SiteStrategy]:
    """Parse sites.js and return {domain: SiteStrategy} map."""
    path = path or SITES_JS_DEFAULT
    text = path.read_text(encoding="utf-8")

    text = re.sub(r"^var defaultSites\s*=\s*", "", text.strip())
    text = re.sub(r";\s*$", "", text)
    text = re.sub(r"^var grouped_sites\s*=\s*\{.*?\};\s*", "", text, flags=re.DOTALL)

    entries = _extract_entries(text)
    return entries_to_domain_map(entries)


_KNOWN_PROP_KEYS = frozenset({
    "domain", "useragent", "useragent_custom", "referer", "referer_custom",
    "random_ip", "allow_cookies", "block_regex", "block_regex_str",
    "block_regex_general", "excluded_domains", "cs_dompurify", "amp", "group", "name",
})


def _build_strategy(domain: str, name: str, props: dict) -> SiteStrategy:
    extra = {
        k: v for k, v in props.items()
        if k not in _KNOWN_PROP_KEYS and not k.startswith("_")
    }
    return SiteStrategy(
        domain=domain,
        name=name,
        useragent=str(props.get("useragent") or ""),
        useragent_custom=str(props.get("useragent_custom") or ""),
        referer=str(props.get("referer") or ""),
        referer_custom=str(props.get("referer_custom") or ""),
        random_ip=str(props.get("random_ip") or ""),
        allow_cookies=bool(props.get("allow_cookies")),
        block_regex=str(props.get("block_regex_str") or props.get("block_regex") or ""),
        block_regex_general=str(props.get("block_regex_general") or ""),
        excluded_domains=list(props.get("excluded_domains") or []),
        cs_dompurify=bool(props.get("cs_dompurify")),
        amp=bool(props.get("amp")),
        group=list(props.get("group") or []),
        extra=extra,
    )


def entries_to_domain_map(entries: dict[str, dict]) -> dict[str, SiteStrategy]:
    """Expand site-name entries (incl. ### groups + exception overrides) to domain map.

    Used by parse_sites_js and rules merge (§15.1.1).
    """
    result: dict[str, SiteStrategy] = {}
    for name, props in entries.items():
        if not isinstance(props, dict):
            continue
        domain = str(props.get("domain") or "")
        # group expansion
        group = props.get("group") or []
        if domain.startswith("###") and group:
            for d in group:
                result[d] = _build_strategy(d, name, props)
        elif domain and not domain.startswith("#"):
            result[domain] = _build_strategy(domain, name, props)
        # exception overrides (sites_updated style)
        for ex in props.get("exception") or []:
            if not isinstance(ex, dict):
                continue
            ed = str(ex.get("domain") or "")
            if not ed:
                continue
            # merge base props with exception props (exception wins)
            merged = {**props, **ex}
            result[ed] = _build_strategy(ed, name, merged)

    general_rules: list[tuple[str, list[str]]] = []
    seen_general_rules: set[tuple[str, tuple[str, ...]]] = set()

    for props in entries.values():
        if not isinstance(props, dict):
            continue
        if "group" in props:
            source_domains = props.get("group") or []
        else:
            source_domain = props.get("domain")
            source_domains = [source_domain] if source_domain else []
        if isinstance(source_domains, str):
            source_domains = source_domains.split(",")

        exceptions = props.get("exception") or []
        for source_domain in source_domains:
            source_domain = str(source_domain)
            selected_rule = props
            for exception in exceptions:
                if not isinstance(exception, dict):
                    continue
                exception_domains = exception.get("domain")
                matches = (
                    exception_domains == source_domain
                    if isinstance(exception_domains, str)
                    else source_domain in (exception_domains or [])
                )
                if matches:
                    selected_rule = exception
                    break

            pattern = str(selected_rule.get("block_regex_general") or "")
            if not pattern:
                continue
            materialized = pattern.replace("{domain}", re.escape(source_domain))
            excluded_domains = list(selected_rule.get("excluded_domains") or [])
            key = (materialized, tuple(excluded_domains))
            if key not in seen_general_rules:
                seen_general_rules.add(key)
                general_rules.append((materialized, excluded_domains))

    for target_domain, strategy in result.items():
        for pattern, excluded_domains in general_rules:
            if _domain_is_excluded(target_domain, excluded_domains):
                continue
            if pattern not in strategy.general_block_regexes:
                strategy.general_block_regexes.append(pattern)
    return result


def _domain_is_excluded(domain: str, excluded_domains: list[str]) -> bool:
    target = domain.lower().removeprefix("www.").rstrip(".")
    for excluded in excluded_domains:
        normalized = str(excluded).lower().removeprefix("www.").rstrip(".")
        if normalized and (target == normalized or target.endswith("." + normalized)):
            return True
    return False


def _extract_entries(text: str) -> dict[str, dict]:
    """Extract site entries from JS object literal text.

    Handles regex literals, arrays, strings, numbers, booleans.
    Returns {site_name: {key: value, ...}}.
    """
    entries: dict[str, dict] = {}
    text = text.strip()
    if text.startswith("{"):
        text = text[1:]
    if text.endswith("}"):
        text = text[:-1]

    current_name = None
    current_props: dict = {}
    i = 0
    length = len(text)

    while i < length:
        i = _skip_ws(text, i, length)
        if i >= length:
            break

        if text[i] == '"':
            key, i = _read_string(text, i, length)
            i = _skip_ws(text, i, length)
            if i < length and text[i] == ':':
                i += 1
                i = _skip_ws(text, i, length)
                if i < length and text[i] == '{':
                    props, i = _read_object(text, i, length)
                    entries[key] = props
                else:
                    _, i = _read_value(text, i, length)
            elif i < length and text[i] == ',':
                i += 1
        elif text[i] == ',':
            i += 1
        else:
            i += 1

    return entries


def _skip_ws(text: str, i: int, length: int) -> int:
    while i < length and text[i] in " \t\r\n":
        i += 1
    if i < length - 1 and text[i] == '/' and text[i + 1] == '/':
        while i < length and text[i] != '\n':
            i += 1
        return _skip_ws(text, i, length)
    return i


def _read_string(text: str, i: int, length: int) -> tuple[str, int]:
    quote = text[i]
    i += 1
    start = i
    while i < length:
        if text[i] == '\\':
            i += 2
            continue
        if text[i] == quote:
            return text[start:i], i + 1
        i += 1
    return text[start:], i


def _read_value(text: str, i: int, length: int) -> tuple:
    """Read a JS value: string, number, bool, regex, array."""
    if i >= length:
        return None, i
    ch = text[i]
    if ch in '"\'':
        return _read_string(text, i, length)
    if ch == '/':
        return _read_regex(text, i, length)
    if ch == '[':
        return _read_array(text, i, length)
    if ch == '{':
        return _read_object(text, i, length)
    end = i
    while end < length and text[end] not in ",}\r\n":
        end += 1
    raw = text[i:end].strip()
    if raw == "true":
        return True, end
    if raw == "false":
        return False, end
    try:
        return int(raw), end
    except ValueError:
        return raw, end


def _read_regex(text: str, i: int, length: int) -> tuple[str, int]:
    i += 1
    start = i
    depth = 0
    while i < length:
        if text[i] == '\\':
            i += 2
            continue
        if text[i] == '[':
            depth += 1
        elif text[i] == ']':
            depth -= 1
        elif text[i] == '/' and depth == 0:
            regex_body = text[start:i]
            i += 1
            while i < length and text[i].isalpha():
                i += 1
            return regex_body, i
        i += 1
    return text[start:], i


def _read_array(text: str, i: int, length: int) -> tuple[list, int]:
    i += 1
    items = []
    while i < length:
        i = _skip_ws(text, i, length)
        if i >= length or text[i] == ']':
            return items, i + 1
        if text[i] == ',':
            i += 1
            continue
        val, i = _read_value(text, i, length)
        if val is not None:
            items.append(val)
    return items, i


def _read_object(text: str, i: int, length: int) -> tuple[dict, int]:
    i += 1
    props: dict = {}
    while i < length:
        i = _skip_ws(text, i, length)
        if i >= length or text[i] == '}':
            return props, i + 1
        if text[i] == ',':
            i += 1
            continue
        if text[i] in '"\'':
            key, i = _read_string(text, i, length)
        else:
            end = i
            while end < length and text[end] not in ":,} \t\r\n":
                end += 1
            key = text[i:end]
            i = end
        i = _skip_ws(text, i, length)
        if i < length and text[i] == ':':
            i += 1
            i = _skip_ws(text, i, length)
            val, i = _read_value(text, i, length)
            if key == "block_regex" and isinstance(val, str):
                props["block_regex_str"] = val
            else:
                props[key] = val
    return props, i


def get_sites_map(sites_js_path: Path | None = None) -> dict[str, SiteStrategy]:
    """Get or build the sites strategy map. Caches to JSON for speed."""
    cache_path = (sites_js_path or SITES_JS_DEFAULT).parent / "sites_cache.json"
    js_path = sites_js_path or SITES_JS_DEFAULT

    if cache_path.exists() and cache_path.stat().st_mtime >= js_path.stat().st_mtime:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return {k: strategy_from_dict(v) for k, v in data.items()}

    sites = parse_sites_js(js_path)
    cache_data = {k: asdict(v) for k, v in sites.items()}
    cache_path.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return sites


PUBLIC_SUFFIX_LIST_VERSION = "2026-08-17_18-44-50_UTC"
PUBLIC_SUFFIX_LIST_COMMIT = "fe5aa073ba579b9d5ae92958b63a7d1de8c13e3a"


class _PSLNode:
    __slots__ = ("children", "terminal", "exception")

    def __init__(self) -> None:
        self.children: dict[str, _PSLNode] = {}
        self.terminal = False
        self.exception = False


class _PublicSuffixTrie:
    """Offline Public Suffix List resolver supporting exact, wildcard and exception rules."""

    def __init__(self, rules: list[str]) -> None:
        self._root = _PSLNode()
        for raw_rule in rules:
            rule = raw_rule.strip()
            if not rule or rule.startswith("//"):
                continue
            exception = rule.startswith("!")
            if exception:
                rule = rule[1:]
            labels = [_normalize_dns_label(label) for label in rule.split(".")]
            if not labels or any(not label for label in labels):
                continue
            node = self._root
            for label in reversed(labels):
                node = node.children.setdefault(label, _PSLNode())
            node.terminal = True
            node.exception = exception

    def public_suffix_length(self, labels: list[str]) -> int:
        """Return the number of labels in the prevailing PSL rule's public suffix."""

        if not labels:
            return 0
        best_match = 1  # implicit prevailing rule "*"
        exception_match = 0
        node = self._root
        for depth, label in enumerate(reversed(labels), start=1):
            wildcard = node.children.get("*")
            if wildcard is not None and wildcard.terminal:
                best_match = max(best_match, depth)

            child = node.children.get(label)
            if child is None:
                break
            node = child
            if node.terminal:
                best_match = max(best_match, depth)
            if node.exception:
                exception_match = depth
                break

        if exception_match:
            return max(1, exception_match - 1)
        return min(best_match, len(labels))

    def registrable_domain(self, host: str) -> str | None:
        presentation = _presentation_hostname(host)
        normalized = _normalize_hostname(host)
        if not normalized or not presentation:
            return None
        try:
            ipaddress.ip_address(normalized)
        except ValueError:
            pass
        else:
            return normalized

        lookup_labels = normalized.split(".")
        presentation_labels = presentation.split(".")
        if len(lookup_labels) != len(presentation_labels) or any(not label for label in lookup_labels):
            return None
        suffix_length = self.public_suffix_length(lookup_labels)
        if suffix_length <= 0 or len(lookup_labels) <= suffix_length:
            return None
        return ".".join(presentation_labels[-(suffix_length + 1):])


def _normalize_dns_label(label: str) -> str:
    value = (label or "").strip().casefold()
    if value == "*":
        return value
    try:
        return value.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        return value


def _presentation_hostname(host: str) -> str:
    value = (host or "").strip().rstrip(".")
    value = value.replace("\u3002", ".").replace("\uff0e", ".").replace("\uff61", ".")
    return value.casefold()


def _normalize_hostname(host: str) -> str:
    value = _presentation_hostname(host)
    if not value:
        return ""
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    labels = value.split(".")
    if any(not label for label in labels):
        return ""
    return ".".join(_normalize_dns_label(label) for label in labels)


@lru_cache(maxsize=1)
def _public_suffix_trie() -> _PublicSuffixTrie:
    try:
        text = resources.files("bpc_fetch").joinpath("data", "public_suffix_list.dat").read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise RuntimeError("packaged Public Suffix List is missing") from exc
    version_marker = f"// VERSION: {PUBLIC_SUFFIX_LIST_VERSION}"
    commit_marker = f"// COMMIT: {PUBLIC_SUFFIX_LIST_COMMIT}"
    if version_marker not in text or commit_marker not in text:
        raise RuntimeError("packaged Public Suffix List version does not match the code pin")
    return _PublicSuffixTrie(text.splitlines())


def domain_from_url(url: str) -> str:
    """Return the registrable domain using the vendored, zero-network Public Suffix List."""

    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    return _public_suffix_trie().registrable_domain(host) or _presentation_hostname(host)
