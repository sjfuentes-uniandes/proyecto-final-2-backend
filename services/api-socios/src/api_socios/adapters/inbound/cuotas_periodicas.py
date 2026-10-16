"""HU-W28: publica PartnerQuotaUsage cada QUOTA_METRIC_INTERVAL_S para las alarmas de cuota."""

import asyncio
import logging
from collections.abc import Callable

logger = logging.getLogger("api_socios.cuotas_periodicas")


async def publicar_periodicamente(publicar: Callable[[], int], intervalo_s: float) -> None:
    while True:
        try:
            socios = await asyncio.to_thread(publicar)
            logger.info("cuota de socios publicada", extra={"socios": socios})
        except Exception as error:  # noqa: BLE001 - la siguiente vuelta lo reintenta
            logger.warning(
                "no se pudo publicar la cuota de socios", extra={"error_type": type(error).__name__}
            )
        await asyncio.sleep(intervalo_s)
