# Implementation Plan: Slice `matching` con desvío exacto (OSRM)

**Branch**: `001-osrm-matching` | **Date**: 2026-10-24 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/001-osrm-matching/spec.md`

> Nota: el script de spec-kit reporta la rama `001-osrm-matching`, pero el repo sigue en `main` (no hay extensión git configurada). Crear la rama antes de implementar.

## Summary

Añadir a `matching-py` un endpoint interno `POST /match` (FastAPI, mismo proceso y event loop que el consumer de preferences) que:

1. Valida el request y el bbox de Yucatán (422).
2. En paralelo: obtiene candidatos con **una** query PostGIS sobre `rides` (filtros de estado, asientos, ventana, conductor, match activo, corredor y dirección; tope 80), lee `user_preferences.structured` y ajusta P y D con OSRM `/nearest`.
3. Calcula `t(A→P)`, `t(P→D)` y `t(D→B)` de todos los candidatos con **una** llamada a OSRM `/table` (82×82 cabe con `max-table-size=100`; se parte en lotes solo si hiciera falta) y descarta por falta de ruta o por desvío mayor al permitido (el del conductor o el del pasajero).
4. Puntúa con `heuristic-v1` (0.5/0.3/0.2), ordena, recorta a `limit` y pide `/route` A→P→D→B para el top 3.
5. Responde y publica `match.shown` en `rides.events` sin bloquear la respuesta.

La ruta original del ride es de crud-ts: aquí solo se lee. El slice no escribe en la DB ni llama a Bedrock.

## Technical Context

**Language/Version**: Python 3.13 (`.python-version`, `requires-python >=3.13`)

**Primary Dependencies**: existentes: pydantic v2, pydantic-settings, asyncpg, redis.asyncio. Nuevas: `fastapi>=0.115,<1`, `uvicorn[standard]>=0.30,<1`, `httpx>=0.27,<1`; dev: `respx`. Sin `shapely` (research R-05).

**Storage**: Postgres + PostGIS compartido, **solo lectura** (`rides`, `matches`, `user_preferences`). Redis Streams `rides.events` (solo XADD).

**Testing**: pytest + pytest-asyncio (ya en el proyecto), respx para OSRM, `ASGITransport` para FastAPI, pruebas de integración opt-in con PostGIS real (`-m integration`).

**Target Platform**: contenedor Linux en ECS Fargate (`infra/task-def-v2.json`, 1 vCPU / 2 GB, puerto 8000).

**Project Type**: servicio web interno (HTTP + consumer de eventos) organizado en vertical slices.

**Performance Goals**: p95 < 400 ms con 80 candidatos (estimado ~80–210 ms, research R-13).

**Constraints**: OSRM con `--max-table-size` = `OSRM_MAX_TABLE_SIZE`; timeouts cortos a OSRM (1 s connect / 2 s read) y 1 reintento antes de responder 503; la publicación de eventos no puede añadir latencia.

**Scale/Scope**: área de Mérida y la península de Yucatán; hasta 80 candidatos por búsqueda; un contenedor (escalar a más tareas tiene un riesgo anotado en R-08).

No quedan NEEDS CLARIFICATION. Los pendientes menores (versión de OSRM, enum de estados de match, zona horaria de `departureAt`) tienen decisión por defecto en research.md y se validan en el quickstart.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` sigue siendo la **plantilla sin llenar**, así que no hay principios formales. Como gates uso las convenciones que ya siguen el repo y las specs Kiro (`preferences-bedrock`, `matching-osrm`):

| Gate | Fuente | Pre-diseño | Post-diseño |
|------|--------|-----------|-------------|
| G1 Vertical slice: el código nuevo vive en `app/features/matching/` y `app/shared/osrm/`; no toca otros slices salvo importar `StructuredPreferences` | README, Kiro design | ✅ | ✅ |
| G2 Puertos + inyección: `CandidateSource` y `RankingStrategy` como `Protocol`; handler sin imports de infraestructura concreta | Kiro preferences RF-10 | ✅ | ✅ |
| G3 Tablas de crud-ts de solo lectura; sin DDL sobre ellas; no crear ni recalcular `rides.route` | Kiro C-03/C-07/C-08, spec FR-023a/b | ✅ | ✅ (el DDL de pruebas vive en `tests/fixtures/`) |
| G4 Bedrock fuera del camino síncrono | Kiro preferences C-02, spec FR-024 | ✅ | ✅ |
| G5 Configuración vía `Settings` (pydantic-settings) | Kiro NF-04 | ✅ | ✅ |
| G6 Pruebas sin servicios externos reales por defecto | Kiro preferences (Stubber/fakes) | ✅ | ✅ (las de integración son opt-in) |
| G7 Logs JSON estructurados | Kiro NF-05 | ✅ | ✅ |

Resultado: **PASS**, sin violaciones. Recomiendo correr `/speckit-constitution` para dejar estos gates formalizados.

## Project Structure

### Documentation (this feature)

```text
specs/001-osrm-matching/
├── spec.md
├── input-technical-notes.md
├── plan.md                 # este archivo
├── research.md             # Fase 0
├── data-model.md           # Fase 1
├── quickstart.md           # Fase 1
├── contracts/
│   ├── match-api.openapi.yaml
│   └── match-shown.event.schema.json
├── checklists/requirements.md
└── tasks.md                # Fase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
app/
├── features/
│   ├── preferences/                 # existente; solo se importa schema.StructuredPreferences
│   └── matching/                    # NUEVO
│       ├── __init__.py
│       ├── router.py                # FastAPI APIRouter: POST /match (orquestación, mapeo 422/503)
│       ├── schema.py                # MatchRequest, MatchResponse, Candidate, MatchShown*
│       ├── candidates.py            # Protocol CandidateSource + PostGISCandidateSource (query única)
│       ├── preferences.py           # lectura de user_preferences.structured → StructuredPreferences | None
│       ├── detour.py                # matriz [A…,P,D,B…], lotes, descarte con motivo
│       ├── scoring.py               # Protocol RankingStrategy + HeuristicV1
│       └── events.py                # build + publish match.shown (fire-and-forget)
├── shared/
│   ├── config.py                    # + variables de data-model §5
│   ├── http/                        # NUEVO
│   │   ├── __init__.py
│   │   └── app.py                   # create_app(deps) + /health + lifespan
│   ├── osrm/                        # NUEVO
│   │   ├── __init__.py
│   │   ├── types.py                 # LatLng, SnappedPoint, Matrix, Route
│   │   ├── errors.py                # OsrmError, OsrmUnavailable, OsrmNoSegment
│   │   └── client.py                # OsrmClient (nearest/table/route), lon,lat solo aquí
│   ├── events/                      # existente (consumer); sin cambios
│   └── llm/                         # existente; matching NO lo importa
main.py                              # gather(uvicorn.Server.serve(), run_consumer(...))
Dockerfile                           # instalar desde pyproject, EXPOSE 8000
tests/
├── fixtures/crud_ts_schema.sql      # réplica de esquema SOLO para pruebas
├── unit/
│   ├── matching/
│   │   ├── test_detour.py           # índices de matriz, null, límites, lotes, desvío negativo
│   │   ├── test_scoring.py          # fórmula, pesos, desempates, rango [0,1], timeWindow=0
│   │   ├── test_router.py           # 200/422/503/[] + match.shown con fakes
│   │   └── test_events.py           # envelope válido contra el JSON Schema; fallo no propaga
│   └── shared/test_osrm_client.py   # respx: lon/lat, null, TooBig, NoSegment, reintento, 503
└── integration/
    └── matching/test_candidates_postgis.py   # escenarios R1–R7 del quickstart
```

**Structure Decision**: un solo proyecto con vertical slices, igual al existente. Me aparto del design de Kiro en esto:
- `handler.py` pasa a llamarse `router.py`, para no confundirlo con el handler de eventos de preferences.
- `repository.py` pasa a `preferences.py`, que solo lee preferencias; los rides y matches los resuelve la query de candidatos.
- La publicación del evento va aparte, en `events.py`.
- La app HTTP va en `shared/http/`, porque la reutilizarán otros slices.

## Diseño de flujo (`POST /match`)

```text
validate(req, bbox)                                         → 422
├─ gather:
│   ├─ candidates.fetch(...)          (1 conexión pool)
│   ├─ preferences.get(passengerId)   (1 conexión pool)
│   └─ osrm.nearest(P), osrm.nearest(D)                     → 503 si OsrmUnavailable; [] si NoSegment
├─ if no rides → 200 []
├─ detour.compute(rides, P', D', prefs, osrm)  → 1×/table (o lotes)  → descartes por motivo
├─ ranker.score(viable) → sort → [:limit] → rank 1..n
├─ gather osrm.route(A,P',D',B) para top-K (fallo → geometry null)
├─ events.publish_match_shown(...)  (create_task, guardado en set)
└─ log JSON: requestId, counts{generated, no_route, detour_driver, detour_passenger, returned}, ms por fase
```

## Seguimiento con la spec Kiro (`.kiro/specs/matching-osrm/`)

Hay diferencias con lo que acabo de diseñar. Conviene alinearla antes de implementar para que las dos specs no se contradigan:

1. **Lotes de `/table`**: Kiro dice 49 candidatos por lote (regla por coordenadas). Este plan usa `sources × destinations ≤ N²`, lo que da hasta 98 candidatos, así que 80 caben en una sola llamada (research R-03).
2. **Timeouts de OSRM**: Kiro NF-02 pone 3 s / 10 s; este plan, 1 s / 2 s (R-07).
3. **`shapely`**: se elimina (R-05).
4. **`match.shown`**: `aggregateType` cambia de `"match"` a `"match_request"`, y `data` incluye `timeWindowMin`, `pickup` y `dropoff` ajustados (contrato en `contracts/`).
5. **`/health`** y el healthCheck sin `curl` (R-02), que la spec Kiro no menciona.
6. **Nombres de archivos**: `router.py`, `preferences.py`, `events.py`, `shared/http/`.

## Riesgos (de infra, fuera de alcance; se reportan)

- `OSRM_URL=http://osrm:5000` en Fargate necesita Service Connect o Cloud Map (R-14).
- El healthCheck con `curl` falla en `python:3.13-slim` (R-02). **Bloquea el despliegue** si no se corrige.
- `REDIS_CONSUMER_NAME` es fijo y choca si se escala a más de una tarea (R-08).
- El rendimiento depende de índices en `rides` que tiene que crear crud-ts (R-06).

## Complexity Tracking

Sin violaciones de los gates; no aplica.
