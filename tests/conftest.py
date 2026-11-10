import os

# Tests never touch real services: force demo mode (load_dotenv does not override variables that
# are already set), use an in-memory database, no background scheduler, no voice keys.
os.environ["PAYPAL_CLIENT_ID"] = ""
os.environ["PAYPAL_CLIENT_SECRET"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-used"
os.environ["DP_DB_PATH"] = ":memory:"
os.environ["DP_SCHEDULER"] = "0"
os.environ["DP_FX"] = "0"  # no network for the reference exchange rate
os.environ["FISH_API_KEY"] = ""
os.environ["FISH_VOICE_ID"] = ""

import pytest  # noqa: E402

from app.business import Business  # noqa: E402
from app.fake import FakePayPal  # noqa: E402
from app.store import Store  # noqa: E402


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def make_biz(store):
    paypal = FakePayPal(store)
    counter = iter(range(1000))

    def make(seed=False, tz="America/Lima"):
        ws = f"{next(counter):032x}"
        store.create_workspace(ws, "Test Shop", "es", tz, "USD", seed)
        return Business(store, paypal, ws)

    return make


@pytest.fixture
def biz(make_biz):
    return make_biz()
