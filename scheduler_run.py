"""
Headless runner for the "Sync & Process Sheet" button.

GitHub Actions runs this file on a schedule. It loads the functions of your Streamlit app WITHOUT opening
any page (everything before the page/UI code), then runs exactly the same sync_google_sheet_batch() that the
sidebar button runs. Your app.py is not modified.

Environment variables
  STREAMLIT_SECRETS   the full content of your Streamlit secrets (TOML). Or put it in .streamlit/secrets.toml
  APP_FILE            your app file name (default app.py)
  DEFAULT_CAMPAIGN    campaign used for rows with an empty campaign cell (default: empty)
  ALLOW_DEFAULT_RULES set to 1 to run even when your saved rules cannot be loaded (not recommended)
"""
import os
import sys
import types
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:          # Python < 3.11
    import tomli as tomllib


# ------------------------------------------------------------------
# 1. A tiny stand-in for streamlit (no page, but secrets + session state work)
# ------------------------------------------------------------------
class _Noop:
    def __call__(self, *a, **k):
        return _Noop()

    def __getattr__(self, name):
        return _Noop()

    def __getitem__(self, key):
        return _Noop()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False


class _State(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value

    def __delattr__(self, name):
        self.pop(name, None)


def _cache(*args, **kwargs):
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]
    return lambda fn: fn


def _load_secrets():
    text = os.environ.get("STREAMLIT_SECRETS", "").strip()
    if not text:
        path = Path(".streamlit/secrets.toml")
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    if not text:
        raise SystemExit("No secrets found. Add the GitHub secret STREAMLIT_SECRETS (your full Streamlit secrets TOML).")
    return tomllib.loads(text)


class _FakeStreamlit(types.ModuleType):
    def __getattr__(self, name):
        return _Noop()


def _install_fake_streamlit():
    fake = _FakeStreamlit("streamlit")
    fake.secrets = _load_secrets()
    fake.session_state = _State()
    fake.cache_resource = _cache
    fake.cache_data = _cache
    components = _FakeStreamlit("streamlit.components")
    components_v1 = _FakeStreamlit("streamlit.components.v1")
    fake.components = components
    components.v1 = components_v1
    sys.modules["streamlit"] = fake
    sys.modules["streamlit.components"] = components
    sys.modules["streamlit.components.v1"] = components_v1
    return fake


def _default_http_timeout(seconds=(10, 120)):
    """Google Sheets (gspread) has no timeout of its own, so one stuck connection could freeze a run for an hour.
    Give every requests-based call a default timeout (calls that set their own timeout keep it)."""
    try:
        import requests
    except ImportError:
        return
    original = requests.sessions.Session.request

    def request(self, method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = seconds
        return original(self, method, url, **kwargs)

    requests.sessions.Session.request = request


_default_http_timeout()


# ------------------------------------------------------------------
# 2. Load the app's functions (everything before the page code)
# ------------------------------------------------------------------
UI_MARKERS = (
    "# ===== UI STARTS HERE",                      # optional marker you can add to app.py
    '\nif st.session_state.theme_mode == "light":',  # first line of the page code in the current app
)


def _load_app(app_file):
    source = Path(app_file).read_text(encoding="utf-8")
    cut = -1
    for marker in UI_MARKERS:
        cut = source.find(marker)
        if cut != -1:
            break
    if cut == -1:
        raise SystemExit("Could not find where the page code starts in " + app_file +
                         ". Add a line  # ===== UI STARTS HERE =====  just before the page code and try again.")
    namespace = {"__name__": "sheet_scheduler_app", "__file__": app_file}
    exec(compile(source[:cut], app_file, "exec"), namespace)
    return namespace


def _progress(event):
    kind = event.get("event")
    if kind == "start":
        print(f"Rows waiting: {event.get('total')}", flush=True)
    elif kind == "row":
        print(f"  row {event.get('row')} ({event.get('position')}/{event.get('total')}): {event.get('label', '')}", flush=True)


def main():
    app_file = os.environ.get("APP_FILE", "app.py")
    os.makedirs("model", exist_ok=True)               # skips the big speech-model download (not needed here)
    Path("qc_config.json").unlink(missing_ok=True)    # always take your latest saved rules from the Google Sheet backup

    _install_fake_streamlit()
    app = _load_app(app_file)

    if not Path("qc_config.json").exists() and os.environ.get("ALLOW_DEFAULT_RULES") != "1":
        print("STOPPED: could not load your saved rules from the Google Sheet backup (tab QC_Config). "
              "Open the app -> Campaign Rules and press Save once, or set ALLOW_DEFAULT_RULES=1.", flush=True)
        return 1

    success, message = app["sync_google_sheet_batch"](os.environ.get("DEFAULT_CAMPAIGN", ""), progress_callback=_progress)
    print(message, flush=True)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
