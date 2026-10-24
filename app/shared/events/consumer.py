"""
Consumer genérico de Redis Streams con XREADGROUP.

Registra handlers por tipo de evento. Solo el slice que conoce
cada tipo debe registrar su handler; el consumer no sabe nada
del dominio.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Awaitable
from typing import Any

logger = logging.getLogger(__name__)

# Tiempo mínimo de inactividad (ms) para XAUTOCLAIM al arrancar
_AUTOCLAIM_MIN_IDLE_MS = 60_000
# Mensajes por iteración
_BATCH_SIZE = 10
# Tiempo de bloqueo en XREADGROUP (ms)
_BLOCK_MS = 5_000


async def run_consumer(
    redis: Any,
    stream: str,
    group: str,
    consumer_name: str,
    handlers: dict[str, Callable[[str], Awaitable[None]]],
) -> None:
    """
    Loop principal del consumer group.

    handlers: dict que mapea `envelope.type` → coroutine handler(raw_json).
    El handler recibe el JSON crudo del campo "envelope" del mensaje.
    El ACK se realiza DESPUÉS de que el handler complete sin excepción.
    Si el handler lanza, el mensaje NO recibe ACK (se reintentará).
    """
    await _ensure_group(redis, stream, group)
    await _autoclaim_pending(redis, stream, group, consumer_name, handlers)

    logger.info("consumer arrancado | stream=%s group=%s consumer=%s", stream, group, consumer_name)

    while True:
        try:
            results = await redis.xreadgroup(
                group,
                consumer_name,
                {stream: ">"},
                count=_BATCH_SIZE,
                block=_BLOCK_MS,
            )
            if not results:
                continue

            for _stream_name, entries in results:
                for msg_id, fields in entries:
                    await _process_message(redis, stream, group, msg_id, fields, handlers)

        except asyncio.CancelledError:
            logger.info("consumer cancelado, saliendo")
            raise
        except Exception:
            logger.exception("error en loop del consumer, reintentando en 5s")
            await asyncio.sleep(5)


async def _ensure_group(redis: Any, stream: str, group: str) -> None:
    """Crea el consumer group si no existe (MKSTREAM crea el stream si falta)."""
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
        logger.info("consumer group creado | stream=%s group=%s", stream, group)
    except Exception as exc:
        # BUSYGROUP significa que ya existe — es OK
        if "BUSYGROUP" in str(exc):
            logger.debug("consumer group ya existe | stream=%s group=%s", stream, group)
        else:
            raise


async def _autoclaim_pending(
    redis: Any,
    stream: str,
    group: str,
    consumer_name: str,
    handlers: dict[str, Callable[[str], Awaitable[None]]],
) -> None:
    """
    Reclama mensajes pendientes de otros consumidores al arrancar.
    Usa XAUTOCLAIM con min-idle-time para recuperar mensajes huérfanos.
    """
    start_id = "0-0"
    while True:
        try:
            # XAUTOCLAIM retorna (next_start_id, entries, deleted_ids)
            next_id, entries, _ = await redis.xautoclaim(
                stream,
                group,
                consumer_name,
                min_idle_time=_AUTOCLAIM_MIN_IDLE_MS,
                start_id=start_id,
                count=_BATCH_SIZE,
            )
            if not entries:
                break

            for msg_id, fields in entries:
                await _process_message(redis, stream, group, msg_id, fields, handlers)

            if next_id in (b"0-0", "0-0"):
                break
            start_id = next_id

        except Exception:
            logger.exception("error durante XAUTOCLAIM, continuando sin recuperar pendientes")
            break


async def _process_message(
    redis: Any,
    stream: str,
    group: str,
    msg_id: Any,
    fields: dict,
    handlers: dict[str, Callable[[str], Awaitable[None]]],
) -> None:
    """
    Parsea el tipo del envelope, despacha al handler correcto y hace ACK.
    Si no hay handler registrado para el tipo, hace ACK inmediato.
    """
    raw_envelope: bytes | str = fields.get(b"envelope") or fields.get("envelope", "{}")

    try:
        event_type = json.loads(raw_envelope).get("type", "")
    except (json.JSONDecodeError, TypeError):
        logger.warning("mensaje con envelope inválido, haciendo ACK | msg_id=%s", msg_id)
        await redis.xack(stream, group, msg_id)
        return

    handler = handlers.get(event_type)
    if handler is None:
        # Tipo no registrado en este slice → ACK sin procesar
        logger.debug("tipo ignorado | type=%s msg_id=%s", event_type, msg_id)
        await redis.xack(stream, group, msg_id)
        return

    try:
        raw_str = raw_envelope.decode() if isinstance(raw_envelope, bytes) else raw_envelope
        await handler(raw_str)
        await redis.xack(stream, group, msg_id)
    except Exception:
        logger.exception(
            "error procesando mensaje, NO se hace ACK para reprocesar | msg_id=%s type=%s",
            msg_id,
            event_type,
        )
