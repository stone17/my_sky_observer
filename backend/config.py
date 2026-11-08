import copy
import ipaddress
import json
import os
import re
import sys
import yaml

CACHE_DIR = "image_cache"
SETTINGS_USER_FILE = "settings_user.yaml"
SETTINGS_DEFAULT_FILE = "settings_default.yaml"
SETTINGS_JSON_LEGACY = "settings.json"
COMPONENTS_FILE = "components.yaml"
USER_DATA_DIR = os.path.join(os.path.expanduser("~"), ".my_sky_observer")
FAVORITES_USER_FILE = os.path.join(USER_DATA_DIR, "favorites.yaml")

_HOSTNAME_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$"
)


def validate_nina_host(host: str) -> str:
    """Validate that nina_host is a safe hostname or IP address without URL injection characters."""
    if host is None:
        return "localhost"
    cleaned = str(host).strip()
    if not cleaned:
        return "localhost"

    # Reject any URL schemes, paths, query strings, credentials, or ports
    if any(ch in cleaned for ch in ("/", "\\", "?", "#", "@", " ", "\t", "\r", "\n")):
        raise ValueError(f"Invalid N.I.N.A. host: {host!r}")

    # Check if valid IPv4 or IPv6 address (strip brackets if IPv6 literal)
    ip_candidate = (
        cleaned[1:-1]
        if cleaned.startswith("[") and cleaned.endswith("]")
        else cleaned
    )
    try:
        ip_obj = ipaddress.ip_address(ip_candidate)
        if isinstance(ip_obj, ipaddress.IPv6Address):
            return f"[{ip_obj.compressed}]"
        return str(ip_obj)
    except ValueError:
        pass

    # Disallow colons (ports) in hostnames
    if ":" in cleaned or len(cleaned) > 253 or not _HOSTNAME_RE.match(cleaned):
        raise ValueError(f"Invalid N.I.N.A. hostname or IP address: {host!r}")

    return cleaned

_settings_cache = {
    "user_file": None,
    "user_mtime": None,
    "legacy_mtime": None,
    "favorites_file": None,
    "favorites_mtime": None,
    "data": None,
}


def get_resource_path(relative_path: str) -> str:
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


def _get_file_mtime(path: str):
    try:
        return os.path.getmtime(path) if os.path.exists(path) else None
    except OSError:
        return None


def invalidate_settings_cache():
    _settings_cache["user_file"] = None
    _settings_cache["user_mtime"] = None
    _settings_cache["legacy_mtime"] = None
    _settings_cache["favorites_file"] = None
    _settings_cache["favorites_mtime"] = None
    _settings_cache["data"] = None


def _normalize_favorites_list(raw_favorites) -> list:
    if not isinstance(raw_favorites, list):
        return []
    seen = set()
    normalized = []
    for item in raw_favorites:
        name = str(item).strip() if item is not None else ""
        if name and name not in seen:
            seen.add(name)
            normalized.append(name)
    return normalized


def load_favorites(favorites_file: str = None) -> dict:
    if favorites_file is None:
        favorites_file = FAVORITES_USER_FILE
    result = {"favorites": [], "filter_favorites_only": False}
    if os.path.exists(favorites_file):
        try:
            with open(favorites_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, list):
                result["favorites"] = _normalize_favorites_list(data)
            elif isinstance(data, dict):
                result["favorites"] = _normalize_favorites_list(data.get("favorites", []))
                if "filter_favorites_only" in data:
                    result["filter_favorites_only"] = bool(data["filter_favorites_only"])
        except Exception:
            pass
    return result


def save_favorites(
    favorites: list,
    filter_favorites_only: bool = None,
    favorites_file: str = None,
) -> dict:
    if favorites_file is None:
        favorites_file = FAVORITES_USER_FILE
    existing = load_favorites(favorites_file=favorites_file)
    norm_favs = _normalize_favorites_list(favorites)
    eff_filter = (
        bool(filter_favorites_only)
        if filter_favorites_only is not None
        else existing.get("filter_favorites_only", False)
    )
    payload = {
        "favorites": norm_favs,
        "filter_favorites_only": eff_filter,
    }
    parent_dir = os.path.dirname(os.path.abspath(favorites_file))
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    tmp_file = f"{favorites_file}.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        yaml.dump(payload, f, default_flow_style=False)
    os.replace(tmp_file, favorites_file)

    if _settings_cache["data"] is not None:
        cs = _settings_cache["data"].setdefault("client_settings", {})
        cs["favorites"] = list(norm_favs)
        cs["filter_favorites_only"] = eff_filter
        _settings_cache["favorites_file"] = favorites_file
        _settings_cache["favorites_mtime"] = _get_file_mtime(favorites_file)

    return payload


def load_settings(
    user_file: str = SETTINGS_USER_FILE,
    default_file: str = SETTINGS_DEFAULT_FILE,
    legacy_file: str = SETTINGS_JSON_LEGACY,
    favorites_file: str = None,
) -> dict:
    if favorites_file is None:
        favorites_file = FAVORITES_USER_FILE
    current_mtime = _get_file_mtime(user_file)
    legacy_mtime = _get_file_mtime(legacy_file)
    fav_mtime = _get_file_mtime(favorites_file)
    if (
        _settings_cache["data"] is not None
        and _settings_cache["user_file"] == user_file
        and _settings_cache["user_mtime"] == current_mtime
        and _settings_cache["legacy_mtime"] == legacy_mtime
        and _settings_cache["favorites_file"] == favorites_file
        and _settings_cache["favorites_mtime"] == fav_mtime
    ):
        return copy.deepcopy(_settings_cache["data"])

    settings = {}
    default_path = get_resource_path(default_file)
    if os.path.exists(default_path):
        try:
            with open(default_path, "r") as f:
                settings = yaml.safe_load(f) or {}
        except Exception:
            pass

    migrated_from_legacy = False
    if os.path.exists(user_file):
        try:
            with open(user_file, "r") as f:
                user = yaml.safe_load(f) or {}
                for k, v in user.items():
                    if k == "profiles" and isinstance(v, dict):
                        settings["profiles"] = v
                    elif (
                        isinstance(v, dict)
                        and k in settings
                        and isinstance(settings[k], dict)
                    ):
                        settings[k].update(v)
                    else:
                        settings[k] = v
        except Exception:
            pass
    elif os.path.exists(legacy_file):
        try:
            with open(legacy_file, "r") as f:
                legacy = json.load(f) or {}
                for k, v in legacy.items():
                    if k == "profiles" and isinstance(v, dict):
                        settings["profiles"] = v
                    elif (
                        isinstance(v, dict)
                        and k in settings
                        and isinstance(settings[k], dict)
                    ):
                        settings[k].update(v)
                    else:
                        settings[k] = v
                migrated_from_legacy = True
        except Exception:
            pass

    if "profiles" not in settings or settings["profiles"] is None:
        settings["profiles"] = {}
    if "client_settings" not in settings or settings["client_settings"] is None:
        settings["client_settings"] = {}

    for legacy_key in ("filter_active_alt", "filter_na_alt", "min_hours"):
        if legacy_key in settings:
            settings["client_settings"][legacy_key] = settings.pop(legacy_key)

    if "profiles" in settings and isinstance(settings["profiles"], dict):
        for p in settings["profiles"].values():
            if isinstance(p, dict):
                for non_prof_key in (
                    "catalogs",
                    "min_altitude",
                    "sort_key",
                    "client_settings",
                    "location",
                    "nina_host",
                    "active_profile",
                    "profiles",
                ):
                    p.pop(non_prof_key, None)

    # Sync favorites from dedicated user-folder file (or migrate from settings if file doesn't exist yet)
    cs = settings["client_settings"]
    if os.path.exists(favorites_file):
        fav_data = load_favorites(favorites_file=favorites_file)
        cs["favorites"] = fav_data["favorites"]
        cs["filter_favorites_only"] = fav_data["filter_favorites_only"]
    else:
        existing_favs = _normalize_favorites_list(cs.get("favorites", []))
        existing_filter = bool(cs.get("filter_favorites_only", False))
        cs["favorites"] = existing_favs
        cs["filter_favorites_only"] = existing_filter
        if existing_favs or existing_filter:
            try:
                save_favorites(
                    existing_favs,
                    filter_favorites_only=existing_filter,
                    favorites_file=favorites_file,
                )
            except Exception:
                pass

    if migrated_from_legacy:
        save_settings(settings, user_file=user_file, favorites_file=favorites_file)
        current_mtime = _get_file_mtime(user_file)

    fav_mtime = _get_file_mtime(favorites_file)
    _settings_cache["user_file"] = user_file
    _settings_cache["user_mtime"] = current_mtime
    _settings_cache["legacy_mtime"] = legacy_mtime
    _settings_cache["favorites_file"] = favorites_file
    _settings_cache["favorites_mtime"] = fav_mtime
    _settings_cache["data"] = copy.deepcopy(settings)
    return settings


def save_settings(
    settings: dict,
    user_file: str = SETTINGS_USER_FILE,
    favorites_file: str = None,
):
    if favorites_file is None:
        favorites_file = FAVORITES_USER_FILE

    cs = settings.get("client_settings")
    if isinstance(cs, dict) and ("favorites" in cs or "filter_favorites_only" in cs):
        favs = _normalize_favorites_list(cs.get("favorites", []))
        filt = cs.get("filter_favorites_only")
        cs["favorites"] = favs
        if filt is not None:
            cs["filter_favorites_only"] = bool(filt)
        try:
            save_favorites(
                favs,
                filter_favorites_only=cs.get("filter_favorites_only"),
                favorites_file=favorites_file,
            )
        except Exception:
            pass
    elif os.path.exists(favorites_file):
        fav_data = load_favorites(favorites_file=favorites_file)
        settings.setdefault("client_settings", {})["favorites"] = fav_data["favorites"]
        settings["client_settings"]["filter_favorites_only"] = fav_data[
            "filter_favorites_only"
        ]

    with open(user_file, "w") as f:
        yaml.dump(settings, f, default_flow_style=False)
    _settings_cache["user_file"] = user_file
    _settings_cache["user_mtime"] = _get_file_mtime(user_file)
    _settings_cache["legacy_mtime"] = _get_file_mtime(SETTINGS_JSON_LEGACY)
    _settings_cache["favorites_file"] = favorites_file
    _settings_cache["favorites_mtime"] = _get_file_mtime(favorites_file)
    _settings_cache["data"] = copy.deepcopy(settings)


def merge_settings_update(current_settings: dict, data: dict) -> dict:
    if "catalogs" in data and isinstance(data["catalogs"], str):
        data["catalogs"] = [c.strip() for c in data["catalogs"].split(",") if c.strip()]

    if "nina_host" in data and data["nina_host"]:
        data["nina_host"] = validate_nina_host(data["nina_host"])

    for k, v in data.items():
        if k == "profiles" and isinstance(v, dict):
            current_settings["profiles"] = v
        elif (
            isinstance(v, dict)
            and k in current_settings
            and isinstance(current_settings[k], dict)
        ):
            current_settings[k].update(v)
        else:
            current_settings[k] = v

    for legacy_key in ("filter_active_alt", "filter_na_alt", "min_hours"):
        if legacy_key in current_settings:
            current_settings.setdefault("client_settings", {})[legacy_key] = (
                current_settings.pop(legacy_key)
            )

    if "profiles" in current_settings and isinstance(current_settings["profiles"], dict):
        for p in current_settings["profiles"].values():
            if isinstance(p, dict):
                for non_prof_key in (
                    "catalogs",
                    "min_altitude",
                    "sort_key",
                    "client_settings",
                    "location",
                    "nina_host",
                    "active_profile",
                    "profiles",
                ):
                    p.pop(non_prof_key, None)

    return current_settings


def load_presets(components_file: str = COMPONENTS_FILE) -> dict:
    comp_path = get_resource_path(components_file)
    if os.path.exists(comp_path):
        try:
            with open(comp_path, "r") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}
