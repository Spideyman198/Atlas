# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Odoo 19 Community Edition + the Atlas bootstrap wrapper.
#
# Two targets:
#
#   runtime  What gets published. Multi-architecture, and carries nothing that
#            only the test suite needs.
#   test     `runtime` plus a browser and the websocket client, for the tours.
#            amd64 only, and never published.
#
# The split exists because Google ships Chrome for linux/amd64 and no other
# Linux architecture. Installing it unconditionally made the arm64 half of the
# manifest unbuildable — apt resolved `google-chrome-stable:amd64` against an
# arm64 base and failed on `libasound2:amd64` — so the published image could
# not be built for arm64 at all. Ubuntu Noble offers no usable substitute:
# `chromium` has no candidate, and `chromium-browser` is a snap wrapper that
# cannot start inside a container.
#
# Keeping the browser out of `runtime` is worth doing on its own terms. A
# deployment image should not carry a browser it never runs.
#
# Pinned to a dated tag rather than the floating `19.0`, so a rebuild six months
# from now produces the same image. Bump deliberately, in a reviewed commit.
# ---------------------------------------------------------------------------
ARG ODOO_VERSION=19.0-20260723
FROM odoo:${ODOO_VERSION} AS runtime

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

USER odoo

ENTRYPOINT ["/usr/local/bin/atlas-bootstrap"]
CMD ["odoo"]


# ---------------------------------------------------------------------------
# test — what the addon suite runs in. Built by docker-compose.yml, used by CI
# and `make test-odoo`, and never pushed to a registry.
# ---------------------------------------------------------------------------
FROM runtime AS test

USER root

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
