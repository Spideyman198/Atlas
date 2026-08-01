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

USER odoo

ENTRYPOINT ["/usr/local/bin/atlas-bootstrap"]
CMD ["odoo"]
