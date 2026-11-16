"""Synthetic LinkedIn-like pages for offline tests. All people, companies and
addresses are fictional; markup mirrors the selectors in collector/linkedin/selectors.py."""

from datetime import datetime, timezone
from html import escape

POSTED = datetime(2026, 11, 10, 9, 30, tzinfo=timezone.utc)


def urn_for(when=POSTED, seq=1, kind="activity"):
    """A post URN whose id encodes ``when`` (LinkedIn ids carry ms time in the top bits)."""
    return f"urn:li:{kind}:{(int(when.timestamp() * 1000) << 22) | seq}"


def post_html(urn, text, author="Priya Sharma", headline="Talent Acquisition at Acme Labs",
              relative="2d", see_more=True, data_attr="data-urn", permalink=True):
    body = "<br>".join(escape(line) for line in text.split("\n"))
    toggle = (
        '<button class="feed-shared-inline-show-more-text__see-more-less-toggle" '
        'aria-expanded="false">…see more</button>' if see_more else ""
    )
    link = f'<a class="app-aware-link" href="/feed/update/{urn}/?trk=public_post">{escape(relative)}</a>' if permalink else ""
    return f"""
<li class="reusable-search__result-container">
  <div class="feed-shared-update-v2 artdeco-card" {data_attr}="{urn}">
    <div class="update-components-actor">
      <span class="update-components-actor__title">
        <span aria-hidden="true">{escape(author)}</span><span class="visually-hidden">View {escape(author)}'s profile</span>
      </span>
      <span class="update-components-actor__description"><span aria-hidden="true">{escape(headline)}</span></span>
      <span class="update-components-actor__sub-description"><span aria-hidden="true">{escape(relative)} • </span></span>
      {link}
    </div>
    <div class="feed-shared-inline-show-more-text">
      <div class="update-components-text"><span dir="ltr">{body}</span></div>
      {toggle}
    </div>
  </div>
</li>"""


def search_page(*posts, extra_head="", extra_body=""):
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Search | LinkedIn</title>{extra_head}</head>
<body>
<header id="global-nav" class="global-nav">LinkedIn navigation</header>
<main class="search-results-container"><ul>{''.join(posts)}</ul></main>
{extra_body}
</body></html>"""


NO_RESULTS_PAGE = """<!DOCTYPE html><html><body>
<header id="global-nav">nav</header>
<div class="search-reusable-search-no-results artdeco-card">
  <h2 class="artdeco-empty-state__headline">No results found</h2>
</div></body></html>"""

LOGIN_PAGE = """<!DOCTYPE html><html><body>
<form class="login__form" action="/checkpoint/lg/login-submit" method="post">
  <input id="username" name="session_key"><input id="password" name="session_password" type="password">
</form></body></html>"""

AUTHWALL_PAGE = """<!DOCTYPE html><html><body>
<main class="authwall-join-form">
  <h1>Join LinkedIn</h1>
  <a href="https://www.linkedin.com/signup/cold-join?trk=authwall">Join now</a>
  <a href="https://www.linkedin.com/login?session_redirect=%2Ffeed%2F">Sign in</a>
</main></body></html>"""

# A logged-in home page WITHOUT the historical global-nav marker (the case the
# live Phase 6b login hit). Includes a post linking to another site's /login page,
# which must not be mistaken for a LinkedIn sign-in link.
FEED_PAGE_NO_NAV = """<!DOCTYPE html><html><head><title>Feed | LinkedIn</title></head><body>
<div class="application-outlet"><div class="scaffold-layout">
  <aside class="scaffold-layout__sidebar">Welcome back</aside>
  <main class="scaffold-layout__main">
    <div class="feed-shared-update-v2" data-urn="urn:li:activity:7300000000000000001">
      <div class="update-components-text"><span>Our status page moved: <a href="https://status.example.com/login">status.example.com/login</a></span></div>
    </div>
  </main>
</div></div></body></html>"""

CHECKPOINT_PAGE = """<!DOCTYPE html><html><body>
<form action="/checkpoint/challenge/verify"><div id="captcha-internal"></div></form>
</body></html>"""

UNRECOGNIZED_PAGE = """<!DOCTYPE html><html><head>
<meta name="csrf-token" content="ajax:1234567890123456789">
<script>window.__SESSION__ = {"JSESSIONID": "ajax:1234567890123456789"};</script>
</head><body>
<header id="global-nav">nav</header>
<div class="brand-new-layout"><article class="mystery-post">We are hiring Python developers</article></div>
<code id="bpr-guid-1">{"li_at": "AQEDAR-secret-session-token-value-that-must-not-leak-0123456789abcdef"}</code>
<input type="hidden" name="csrfToken" value="ajax:9876543210987654321">
</body></html>"""

FEED_PAGE = search_page(post_html(urn_for(seq=900), "Just a feed post about our team offsite."))

# ---------------------------------------------------------------------------
# Current LinkedIn layout ("SDUI"), structure observed in the Phase 6b live
# diagnostic. Class names are hashed and meaningless (randomised here on
# purpose); there is no data-urn and no post permalink. All content fictional.
# ---------------------------------------------------------------------------

def _h(seed, n=4):
    """Fake hashed class list, e.g. 'k9x01 k9x02' - extraction must not rely on it."""
    return " ".join(f"z{seed}{i:02d}" for i in range(n))


def sdui_post_html(author="Riya Fictional", headline="Talent Partner at Example Labs",
                   relative="3h", text="We're hiring a Backend Engineer!\nSend your CV to jobs@example.test",
                   slug="riya-fictional-0000", seed="a", more_button=True):
    lines = text.split("\n")
    body = "<br>".join(escape(line) for line in lines)
    more = (f'<button class="{_h(seed, 3)}" aria-hidden="true"><span><span>'
            f'<span class="x">…</span><span> more</span></span></span></button>') if more_button else ""
    time_p = f'<div class="{_h(seed, 3)}"><p class="{_h(seed, 9)}"><span>{escape(relative)} • </span></p></div>' if relative else ""
    return f"""
<div class="{_h(seed, 2)}"><div class="{_h(seed, 4)}"><div class="{_h(seed, 3)}" id="[REDACTED]">
 <div class="{_h(seed, 21)}"><div class="{_h(seed, 2)}"><div class="{_h(seed, 8)}">
  <h2 class="{_h(seed, 6)}"><span class="{_h(seed, 2)}">Feed post</span></h2>
  <div class="{_h(seed, 8)}"><div class="{_h(seed, 10)}"><div class="{_h(seed, 10)}">
   <a class="{_h(seed, 6)}" href="https://www.linkedin.com/in/{slug}/"><figure><img></figure></a>
   <div class="{_h(seed, 5)}"><div class="{_h(seed, 7)}">
    <a class="{_h(seed, 14)}" href="https://www.linkedin.com/in/{slug}/">
     <div><div><div class="{_h(seed, 10)}">
      <div class="{_h(seed, 5)}"><div><p class="{_h(seed, 10)}"><span>{escape(author)}</span></p></div></div>
      <div class="{_h(seed, 3)}"><p class="{_h(seed, 9)}"><span>• 2nd</span></p></div>
     </div></div></div>
    </a>
   </div></div>
   <div class="{_h(seed, 3)}"><p class="{_h(seed, 9)}"><span>{escape(headline)}</span></p></div>
   {time_p}
   <a class="{_h(seed, 12)}" href="https://www.linkedin.com/in/{slug}/"></a>
  </div>
  <div><button class="{_h(seed, 13)}"><span>Follow</span></button></div>
  <button class="{_h(seed, 19)}"></button></div>
  <p class="{_h(seed, 14)}"><span class="{_h(seed, 9)}">{body}
   <a href="https://www.linkedin.com/search/results/all/?keywords=%23hiring">#hiring</a>{more}</span></p>
  <div class="{_h(seed, 10)}">
   <button class="{_h(seed, 13)}"><span>7</span></button>
   <button class="{_h(seed, 16)}" aria-expanded="false"></button>
   <button class="{_h(seed, 13)}" aria-expanded="false"><span>2</span></button>
   <a class="{_h(seed, 13)}" href="https://www.linkedin.com/search/results/content/?keywords=x"><span>3</span></a>
  </div>
 </div></div></div>
</div></div></div>"""


def sdui_search_page(*posts, extra_head="", extra_body=""):
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Search | LinkedIn</title>{extra_head}</head>
<body><div id="root"><header class="zz01 zz02"><h2 class="zz03">0 notifications</h2>
<a href="https://www.linkedin.com/mynetwork">My Network</a></header>
<main id="workspace"><div id="SearchResults_SearchResultsFilterBar">Posts</div>
<div class="zz10 zz11 zz12 zz13 zz14 zz15">{''.join(posts)}</div>
<aside id="SearchResults_SearchRightRail"><h2>Ad Options</h2></aside></main>
{extra_body}</div></body></html>"""


# ---------------------------------------------------------------------------
# Current layout WITH the data-view-name hooks confirmed by the live inventory
# (role="listitem" > data-view-name="feed-full-update" > container > hooks).
# Every hook can be removed or emptied to test fallbacks. Fictional content.
# ---------------------------------------------------------------------------

def dvn_post_html(author="Riya Fictional", headline="Talent Partner at Example Labs", relative="3h",
                  text="We're hiring a Python Backend Developer!\nSend your CV to jobs@example.test",
                  slug="riya-fictional-0000", heading="Feed post", actor=True, commentary=True,
                  cta=None, image_caption=None, nested="", likers=True, more_button=True, seed="d"):
    body = "<br>".join(escape(line) for line in text.split("\n"))
    more = (f'<button class="{_h(seed, 3)}" aria-hidden="true"><span>…</span><span> more</span></button>'
            if more_button else "")
    if commentary is True:
        commentary_html = (f'<p class="{_h(seed, 14)}" data-view-name="feed-commentary">'
                           f'<span class="{_h(seed, 9)}">{body}{more}</span></p>')
    elif commentary == "empty":
        commentary_html = f'<p data-view-name="feed-commentary"><span> {more} </span></p>'
    else:  # hook missing: the same paragraph without data-view-name
        commentary_html = f'<p class="{_h(seed, 14)}"><span class="{_h(seed, 9)}">{body}{more}</span></p>'
    actor_attr = ' data-view-name="feed-actor"' if actor is True else ""
    actor_inner = ("<div><p><span> </span></p></div>" if actor == "empty" else
                   f'<div class="{_h(seed, 5)}"><p class="{_h(seed, 10)}"><span>{escape(author)}</span></p></div>'
                   f'<div class="{_h(seed, 3)}"><p class="{_h(seed, 9)}"><span>• 2nd</span></p></div>')
    if actor == "empty":
        actor_attr = ' data-view-name="feed-actor"'
    heading_html = f'<h2 class="{_h(seed, 6)}"><span>{escape(heading)}</span></h2>' if heading else ""
    cta_html = (f'<a data-view-name="feed-actor-sub-description-cta" href="https://example.test/apply">'
                f'<p>{escape(cta)}</p></a>' if cta else "")
    image_html = (f'<div data-view-name="feed-update-image"><img alt="{escape(image_caption)}">'
                  f'<p>{escape(image_caption)}</p></div>' if image_caption else "")
    likers_html = '<a data-view-name="view-likers" href="#"><span>12</span></a>' if likers else ""
    return f"""
<div role="listitem" componentkey="opaque{seed}aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">
 <div data-view-name="feed-full-update" data-display-contents="true">
  <div class="{_h(seed, 8)}" componentkey="opaque{seed}bbbbbbbbbbbbbbbbbbbbbbbbbbbb">
   {heading_html}
   <a data-view-name="feed-actor-image" href="https://www.linkedin.com/in/{slug}/"><figure><img></figure></a>
   <a{actor_attr} href="https://www.linkedin.com/in/{slug}/">{actor_inner}</a>
   <div><p class="{_h(seed, 9)}"><span>{escape(headline)}</span></p></div>
   <div><p class="{_h(seed, 9)}"><span>{escape(relative)} • </span></p></div>
   {cta_html}
   <button data-view-name="relationship-building-button"><span>Follow</span></button>
   <button data-view-name="edge-creation-follow-action"><span>Connect</span></button>
   <button data-view-name="feed-control-menu" aria-label="Open control menu for post"></button>
   {commentary_html}
   {image_html}
   {nested}
   <div>
    <button data-view-name="reaction-button"><span>Like</span></button>
    <button data-view-name="feed-comment-button"><span>Comment</span></button>
    <button data-view-name="feed-share-button"><span>Repost</span></button>
    <button data-view-name="feed-send-as-message-button"><span>Send</span></button>
    {likers_html}
   </div>
  </div>
 </div>
</div>"""


HIRING_TEXT = (
    "We're hiring a Senior Python Developer at Acme Labs! 🚀\n"
    "Location: Bengaluru (Hybrid)\nExperience: 3-5 years\n"
    "Send your CV to careers@acme.example or WhatsApp +91 98765 43210\n"
    "hashtag#hiring hashtag#python"
)
REACT_TEXT = (
    "Urgent opening for React Developers - immediate joiners preferred.\n"
    "Drop your CV: jobs@initrode.example"
)
NOT_HIRING_TEXT = "Thrilled to share that I started a new position at Initech! Thanks everyone for the support."
