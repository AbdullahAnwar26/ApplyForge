"""
Gunicorn configuration for the private/VPN ApplyForge deployment (Milestone 8.4c).

Loaded by the systemd unit with ``--config /srv/applyforge/current/deploy/gunicorn.conf.py``
and run from ``/srv/applyforge/current/backend``. See ``deploy/README.md``.

Deliberate choices:

* Bind to 127.0.0.1 only. nginx is the sole client; Gunicorn is never reachable
  from the VPN or any other network interface.
* Exactly one worker process with threads. The API's throttles (LocMemCache) and the
  collector's "one run at a time" lock are per process, so more worker processes
  would silently weaken both. Threads provide concurrency for ordinary requests
  while a long request is in progress.
* ``timeout = 900``. With the gthread worker this is NOT a per-request limit: the
  worker's main loop keeps signalling liveness while request threads are busy, so
  it only detects a frozen worker. The effective limit for clients is nginx's
  ``proxy_read_timeout`` (60 s; 900 s for the collector-run and generate paths).
  When nginx gives up, the client gets 504 but Django may still finish the request.
* ``graceful_timeout = 120``. On stop or restart, Gunicorn closes its listening
  socket at once (new requests get 502 from nginx until the new process is ready)
  and gives in-flight requests up to 120 s before killing them.
* Logs go to stdout/stderr, so systemd captures them in the journal
  (``journalctl -u applyforge``). Request URLs are logged; the API token travels
  in the Authorization header, which is never logged.

Nothing in this file is secret. Secrets come from the systemd EnvironmentFile.
"""

# --- Application -----------------------------------------------------------
wsgi_app = "config.wsgi:application"
# Import the app in the worker, not the master: a restart then always picks up
# the code that ``/srv/applyforge/current`` points to.
preload_app = False

# --- Network ---------------------------------------------------------------
bind = ["127.0.0.1:8000"]  # localhost only - never 0.0.0.0 or the VPN address
backlog = 64

# Only nginx on this host may set forwarding headers that Gunicorn trusts.
# Django itself trusts X-Forwarded-Proto via SECURE_PROXY_SSL_HEADER, which is safe
# because nginx always overwrites that header and Gunicorn only listens locally.
forwarded_allow_ips = "127.0.0.1"

# --- Processes -------------------------------------------------------------
workers = 1               # must stay 1: per-process throttles and collector lock
worker_class = "gthread"
threads = 8
worker_connections = 64

# --- Timeouts --------------------------------------------------------------
# Worker-hang detection only (see the module docstring); it does not end long requests.
timeout = 900
# On SIGTERM (systemctl stop/restart): stop accepting at once, let in-flight requests
# run for up to this long, then kill them. The systemd unit's TimeoutStopSec (150 s)
# must stay longer than this.
#
# While the collector and generation are disabled, requests are short and 120 s is
# ample. Generation, once enabled, can run up to OLLAMA_TIMEOUT_SECONDS (180 s by
# default), so it could be cut off by a restart: raise this above that value (e.g.
# 200) and TimeoutStopSec above this one (e.g. 230) when enabling it. A collector run
# can outlast any reasonable grace period: restart only when none is in progress.
graceful_timeout = 120
keepalive = 5             # seconds; nginx -> Gunicorn on localhost

# --- Request limits --------------------------------------------------------
# Upload size itself is limited by nginx (client_max_body_size) and by the
# application (10 MB resume limit); these only bound request line and headers.
limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190

# --- Logging ---------------------------------------------------------------
accesslog = "-"           # stdout -> journal
errorlog = "-"            # stderr -> journal
loglevel = "info"
capture_output = True     # stray print()/warnings from the app also reach the journal
# Remote address is always 127.0.0.1 (nginx); the real client is in X-Forwarded-For.
access_log_format = '%({x-forwarded-for}i)s "%(r)s" %(s)s %(b)s %(M)sms "%(a)s"'

proc_name = "applyforge"

# --- Control interface -----------------------------------------------------
# Gunicorn >= 25.1 opens a runtime control socket by default ($XDG_RUNTIME_DIR or
# $HOME/.gunicorn/). It can change the worker count at runtime, which would break
# the single-worker guarantee above, and its default path is not writable under
# the systemd sandbox. Manage the service through systemctl instead.
control_socket_disable = True
