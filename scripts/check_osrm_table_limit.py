"""
Diagnóstico: ¿cómo limita el OSRM desplegado el tamaño de /table?

Envía una tabla con la misma forma que usa features/matching/detour.py
(coords = [A₁…Aₙ, P, D, B₁…Bₙ], (n+2) sources × (n+2) destinations) con
puntos aleatorios dentro de Mérida, sin la validación local del cliente.

    uv run python -m scripts.check_osrm_table_limit --n 80
    uv run python -m scripts.check_osrm_table_limit --n 80 --osrm-url http://localhost:5000 --max-table-size 100

Interpretación (research R-03), con --max-table-size N en el contenedor:
- n = N − 2 responde Ok        → regla sources × destinations ≤ N² (la que asume detour.py).
- n = N − 2 responde TooBig     → el contenedor limita por número de coordenadas:
                                  cambiar max_batch_candidates() a (N − 2) // 2.
No importa app.shared.config: no requiere REDIS_URL ni DATABASE_URL.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time

from app.shared.osrm import LatLng, OsrmClient, OsrmError

# Rectángulo aproximado de la mancha urbana de Mérida
_LAT = (20.93, 21.05)
_LNG = (-89.70, -89.55)


def _random_point(rng: random.Random) -> LatLng:
    return LatLng(lat=rng.uniform(*_LAT), lng=rng.uniform(*_LNG))


async def _check(osrm_url: str, max_table_size: int, n: int, seed: int) -> int:
    rng = random.Random(seed)
    coords = [_random_point(rng) for _ in range(2 * n + 2)]
    sources = list(range(n + 2))
    destinations = [n, n + 1] + list(range(n + 2, 2 * n + 2))

    client = OsrmClient(osrm_url, max_table_size=max_table_size, connect_timeout_s=3, read_timeout_s=30)
    print(f"OSRM={osrm_url} n={n} coords={len(coords)} tabla={len(sources)}x{len(destinations)}")
    t0 = time.perf_counter()
    try:
        matrix = await client.table(coords, sources, destinations, validate_size=False)
    except OsrmError as exc:
        print(f"code={exc.code} message={exc.message!r} ms={(time.perf_counter() - t0) * 1000:.0f}")
        return 1
    finally:
        await client.aclose()

    nulls = sum(cell is None for row in matrix.durations for cell in row)
    print(f"code=Ok ms={(time.perf_counter() - t0) * 1000:.0f} celdas_null={nulls}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=80, help="candidatos simulados (default 80)")
    parser.add_argument("--osrm-url", default=os.environ.get("OSRM_URL", "http://localhost:5000"))
    parser.add_argument("--max-table-size", type=int, default=int(os.environ.get("OSRM_MAX_TABLE_SIZE", "100")))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    sys.exit(asyncio.run(_check(args.osrm_url, args.max_table_size, args.n, args.seed)))


if __name__ == "__main__":
    main()
