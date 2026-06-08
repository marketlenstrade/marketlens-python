import httpx

VERSION = "1.3.2"
DEFAULT_BASE_URL = "https://api.marketlens.trade/v1"
DEFAULT_MAX_RETRIES = 2

# Granular default for normal API calls.
DEFAULT_TIMEOUT: httpx.Timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)

# Long-running downloads stream bytes from the server for many minutes on cold
# cache. Read timeout is disabled so the connection isn't dropped while bytes
# are flowing; connect/write/pool stay strict.
DOWNLOAD_TIMEOUT: httpx.Timeout = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0)
