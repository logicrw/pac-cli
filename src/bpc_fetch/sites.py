"""Parse BPC extension sites.js into a strategy map."""
import json
import re
import sys
from pathlib import Path
from dataclasses import dataclass, field, asdict, fields


def _default_sites_js() -> Path:
    """Locate sites.js: PyInstaller bundle → package data → home fallback."""
    if getattr(sys, '_MEIPASS', None):
        return Path(sys._MEIPASS) / "data" / "sites.js"
    pkg_data = Path(__file__).parent.parent.parent / "data" / "sites.js"
    if pkg_data.exists():
        return pkg_data
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
    "cs_dompurify", "amp", "group", "name",
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
    return result


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


def domain_from_url(url: str) -> str:
    """Extract registrable domain from URL."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # Handle two-part TLDs: .co.uk, .com.au, .co.jp, .com.br, etc.
    two_part_tlds = {"co.uk", "com.au", "co.nz", "co.jp", "co.kr", "com.br",
                     "co.za", "co.il", "com.sg", "com.tw", "com.ar", "com.uy",
                     "com.mx", "com.pe", "com.bo", "com.co", "co.ke", "com.pl",
                     "org.il", "org.uk", "net.au", "com.es", "co.in"}
    suffix = ".".join(parts[-2:])
    if suffix in two_part_tlds:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
