# Safety net: no collector test may open a browser that can reach the network.
# With this off, LinkedInBrowser only starts when a route_handler serves every
# request from offline fixtures (see test_linkedin_browser.py).
from collector.linkedin import browser as _browser

_browser.LIVE_BROWSER_ALLOWED = False
