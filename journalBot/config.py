import os
from dataclasses import dataclass
from urllib.parse import urlparse

from dotenv import load_dotenv


@dataclass
class ProxyConfig:
    server: str
    username: str | None = None
    password: str | None = None


@dataclass
class Settings:
    google_sheets_id: str
    google_service_account_json: str
    profiles: list[str]
    target_profile_url: str
    headless: bool
    profiles_dir: str
    profile_proxies: dict[str, ProxyConfig]


def _parse_proxy_value(raw_proxy: str) -> ProxyConfig:
    raw_proxy = raw_proxy.strip()
    parsed = urlparse(raw_proxy)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        raise ValueError(
            "Proxy must be full URL, e.g. http://user:pass@host:port or socks5://host:port"
        )

    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    return ProxyConfig(
        server=server,
        username=parsed.username,
        password=parsed.password,
    )


def _parse_profile_proxy_map(raw_map: str) -> dict[str, ProxyConfig]:
    out: dict[str, ProxyConfig] = {}
    if not raw_map.strip():
        return out

    chunks = [item.strip() for item in raw_map.split(";") if item.strip()]
    for chunk in chunks:
        if "=" not in chunk:
            raise ValueError(
                "PROFILE_PROXY_MAP format error. Use profile_id=proxy_url;profile_id=proxy_url"
            )
        profile_id, proxy_url = chunk.split("=", 1)
        profile_id = profile_id.strip()
        if not profile_id:
            raise ValueError("PROFILE_PROXY_MAP contains empty profile id")
        out[profile_id] = _parse_proxy_value(proxy_url)
    return out


def load_settings() -> Settings:
    load_dotenv()

    sheets_id = os.getenv("GOOGLE_SHEETS_ID", "").strip()
    service_account = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    raw_profiles = os.getenv("PROFILES", "").strip()
    target_profile_url = os.getenv("TARGET_PROFILE_URL", "").strip()
    headless = os.getenv("HEADLESS", "false").lower() == "true"
    profiles_dir = os.getenv("PROFILES_DIR", "browser_profiles").strip()
    raw_proxy_map = os.getenv("PROFILE_PROXY_MAP", "").strip()

    profiles = [item.strip() for item in raw_profiles.split(",") if item.strip()]
    proxy_map = _parse_profile_proxy_map(raw_proxy_map)

    missing = []
    if not sheets_id:
        missing.append("GOOGLE_SHEETS_ID")
    if not service_account:
        missing.append("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not profiles:
        missing.append("PROFILES")
    if not target_profile_url:
        missing.append("TARGET_PROFILE_URL")
    if not profiles_dir:
        missing.append("PROFILES_DIR")

    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"Missing required env variables: {joined}")

    return Settings(
        google_sheets_id=sheets_id,
        google_service_account_json=service_account,
        profiles=profiles,
        target_profile_url=target_profile_url,
        headless=headless,
        profiles_dir=profiles_dir,
        profile_proxies=proxy_map,
    )
