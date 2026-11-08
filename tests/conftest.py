import os

# Tests never touch real services: force demo mode (load_dotenv does not override variables that
# are already set) and give the SDK a placeholder key; every test injects a scripted Claude client.
os.environ["PAYPAL_CLIENT_ID"] = ""
os.environ["PAYPAL_CLIENT_SECRET"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-used"
