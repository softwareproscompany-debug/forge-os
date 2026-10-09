"""Dev outbox: where stub channel providers record "sent" messages.

:func:`DevOutbox.record` imports ``forge_db`` lazily (only when called) so this
package imports cleanly even when the ``forge-db`` package isn't installed yet.
It expects ``forge_db`` to expose a ``DevOutbox`` model — either as
``forge_db.models.DevOutbox`` (the api workstream's layout per CONTRACTS.md)
or as a top-level ``forge_db.DevOutbox`` attribute — and ``db`` to be a
synchronous SQLAlchemy-style session with ``add()``/``commit()``.
"""

from __future__ import annotations

from typing import Any


class DevOutbox:
    """Write-only view of the ``dev_outbox`` table for stub providers."""

    @staticmethod
    def record(
        db: Any,
        business_id: Any,
        channel: str,
        to_address: str,
        subject: str | None,
        body: str,
        provider: str = "stub",
    ) -> Any:
        """Insert one row into ``dev_outbox`` and return the ORM row.

        Args:
            db: synchronous session (``add`` + ``commit``).
            business_id: tenant UUID for the row.
            channel: ``email`` | ``sms`` | ``social``.
            to_address: recipient address/number/handle.
            subject: subject line (email only; None otherwise).
            body: message body.
            provider: provider name recorded on the row (default ``"stub"``).

        Raises:
            RuntimeError: if ``forge_db`` isn't importable or exposes no
                ``DevOutbox`` model.
        """
        try:
            import forge_db  # lazy: forge-db is owned by the api workstream
        except ImportError as exc:
            raise RuntimeError(
                "forge_db is not installed, so the dev outbox cannot be written. "
                "Install the forge-db package (pip install -e packages/forge-db) "
                "or run with stub providers disabled."
            ) from exc

        models = getattr(forge_db, "models", forge_db)
        model = getattr(models, "DevOutbox", None)
        if model is None:
            raise RuntimeError(
                "forge_db exposes no DevOutbox model (looked for "
                "forge_db.models.DevOutbox and forge_db.DevOutbox). The api "
                "workstream owns the forge-db models; this is an integration gap, "
                "not a caller bug."
            )
        row = model(
            business_id=business_id,
            channel=channel,
            to_address=to_address,
            subject=subject,
            body=body,
            provider=provider,
        )
        db.add(row)
        db.commit()
        return row
