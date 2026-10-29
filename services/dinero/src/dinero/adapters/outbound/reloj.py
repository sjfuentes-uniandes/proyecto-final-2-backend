from datetime import UTC, datetime


class RelojSistema:
    def ahora(self) -> datetime:
        return datetime.now(UTC)
