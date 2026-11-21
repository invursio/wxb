# Vendored from Wookhq/Lution @ 303be659833a (MIT, Copyright (c) 2026 WookHQ).
# Import paths are package-relative; everything else is upstream.
# See NOTICE.md next to this package.
# this only works when you launch sober through lution or the desktop shortcut "Sober with Lution"

import json

from . import paths


def config_dir():
    return paths.state_dir()


def env_file():
    return config_dir() / "env_vars.json"


def load_vars():
    try:
        data = json.loads(env_file().read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def valid_key(key):
    key = key.strip()
    if not key or "=" in key or any(c.isspace() for c in key):
        return False
    return True


def save_vars(vars_dict):
    clean = {}
    for key, value in vars_dict.items():
        key = str(key).strip()
        value = str(value).strip()
        if valid_key(key):
            clean[key] = value
    config = config_dir()
    config.mkdir(parents=True, exist_ok=True)
    env_file().write_text(json.dumps(clean, indent=2) + "\n")
    return clean


def env_flatpak_args(env_vars=None):
    if env_vars is None:
        env_vars = load_vars()
    return [f"--env={key}={value}" for key, value in env_vars.items()]


def __getattr__(name):
    if name == "CONFIG_DIR":
        return config_dir()
    if name == "ENV_FILE":
        return env_file()
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
