# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Odoo 19 Community Edition + the Atlas bootstrap wrapper.
#
# Pinned to a dated tag rather than the floating `19.0`, so a rebuild six months
# from now produces the same image. Bump deliberately, in a reviewed commit.
# ---------------------------------------------------------------------------
ARG ODOO_VERSION=19.0-20260723
FROM odoo:${ODOO_VERSION}

LABEL org.opencontainers.image.title="Odoo Atlas — Odoo" \
      org.opencontainers.image.description="Odoo 19 CE with the odoo_atlas addon path" \
      org.opencontainers.image.licenses="LGPL-3.0-or-later"

USER root

# The bootstrap wrapper must be executable inside the image regardless of the
# host filesystem — Windows checkouts do not carry the executable bit.
COPY docker/odoo/bootstrap.sh /usr/local/bin/atlas-bootstrap
RUN chmod 0755 /usr/local/bin/atlas-bootstrap

# Addon Python dependencies are installed here from M5 onward. Nothing to add yet;
# introducing an empty requirements file now would only be ceremony.

# A browser, for the tours. Odoo runs them by driving one over the devtools
# protocol, and without it the chat panel has no automated test at all — its
# acceptance criterion is a user completing a round trip, which cannot be
# asserted from Python alone.
#
# Google's build rather than Ubuntu's `chromium`, which on Noble is a stub that
# installs a snap: it produces a `chromium` on PATH that cannot start inside a
# container. Odoo looks for `google-chrome` first (`odoo/tests/common.py`).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
      | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg]" \
      "https://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends google-chrome-stable \
 && apt-get purge -y curl gnupg \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# `HttpCase` talks to the browser over a websocket. Without this it does not
# fail — it logs a warning and *skips* the test, so a suite full of browser
# tests reports green while running none of them.
RUN pip install --no-cache-dir --break-system-packages websocket-client==1.9.0

USER odoo

ENTRYPOINT ["/usr/local/bin/atlas-bootstrap"]
CMD ["odoo"]
