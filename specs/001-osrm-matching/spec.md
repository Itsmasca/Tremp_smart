# Feature Specification: Slice `matching` con desvío exacto (OSRM)

**Feature Branch**: `001-osrm-matching`

**Created**: 2026-10-24

**Status**: Draft

**Input**: User description: "slice `matching` con OSRM en matching-py — `POST /match`: dado el origen, destino y hora de un pasajero, devolver los rides viables ordenados por relevancia, con desvío exacto calculado con OSRM, y emitir `match.shown` para entrenar el ranker." (texto técnico completo preservado en [input-technical-notes.md](./input-technical-notes.md); el texto original llegó **truncado** tras "Guardar también").

## Contexto

Plataforma de rides comunitarios en Mérida, Yucatán. La app móvil solo habla con `crud-ts`, que reenvía las búsquedas de viaje a `matching-py` por red interna. Este slice es el que decide **qué rides ve un pasajero y en qué orden**. Convive con el slice `preferences` (spec Kiro `.kiro/specs/preferences-bedrock`), del que solo **lee** las preferencias estructuradas ya extraídas; nunca invoca al modelo de lenguaje en el camino síncrono (restricción C-02 de esa spec).

## Clarifications

### Session 2026-10-24

- Q: ¿Cómo se aplican las preferencias del pasajero? → A: Solo `max_detour_minutes` es filtro duro; el resto solo ajusta el score; `gender_preference` se ignora por ahora.
- Q: ¿Fórmula de `heuristic-v1`? → A: Suma ponderada normalizada: 0.5·(1 − desvío/límite) + 0.3·(1 − diferencia de hora/ventana) + 0.2·(1 − ETA/umbral).
- Q: ¿Quién gestiona la ruta del ride? → A: crud-ts la crea y la guarda; matching-py solo la lee y calcula al vuelo el recorrido con el pasajero (FR-023a/b).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Pasajero encuentra rides viables para su trayecto (Priority: P1)

Un pasajero indica dónde quiere que lo recojan, a dónde va y a qué hora quiere salir. El sistema le devuelve, ordenados de más a menos relevante, los rides programados de otros conductores que pasan cerca de ambos puntos, en el sentido correcto, con asientos libres y dentro de su ventana de horario, indicando para cada uno cuánto tiempo extra le cuesta al conductor desviarse y cuánto tardará en llegar al punto de recogida.

**Why this priority**: Es la razón de ser del slice. Sin esto no hay emparejamiento y la app no puede mostrar opciones de viaje.

**Independent Test**: Con una base de datos sembrada con rides conocidos alrededor de Mérida y un motor de rutas con el grafo de Yucatán, enviar una búsqueda y verificar qué rides aparecen, cuáles no, su orden y que los desvíos coinciden con los tiempos de ruta reales.

**Acceptance Scenarios**:

1. **Given** un ride programado con asientos libres cuya ruta pasa a menos de 1.5 km del pickup y del dropoff, en el sentido pickup→dropoff, y con salida dentro de la ventana, **When** el pasajero busca, **Then** el ride aparece con su desvío (segundos extra respecto a la ruta original del conductor) y su tiempo estimado hasta el pickup.
2. **Given** un ride que pasa cerca de ambos puntos pero en sentido contrario (pasa primero por el destino y luego por el origen del pasajero), **When** el pasajero busca, **Then** el ride NO aparece.
3. **Given** un ride cuyo desvío calculado supera el desvío máximo que su conductor aceptó, **When** el pasajero busca, **Then** el ride NO aparece.
4. **Given** rides sin asientos libres, no programados (cancelados, en curso, terminados), fuera de la ventana horaria, publicados por el propio pasajero, o con los que el pasajero ya tiene un match activo, **When** el pasajero busca, **Then** ninguno de ellos aparece.
5. **Given** que ningún ride cumple los criterios, **When** el pasajero busca, **Then** recibe una respuesta exitosa con lista vacía (no un error).
6. **Given** varios rides viables, **When** el pasajero busca, **Then** los resultados vienen ordenados por relevancia descendente, con `rank` consecutivo desde 1, y nunca más que el `limit` solicitado.

---

### User Story 2 - El pasajero ve en el mapa el recorrido de las mejores opciones (Priority: P2)

Para las primeras opciones (por defecto las 3 mejores), la respuesta incluye el trazado completo del recorrido del conductor con el pasajero (origen del conductor → recogida → bajada → destino del conductor) y los puntos de recogida/bajada ajustados a la calle más cercana, para que la app los dibuje en el mapa sin pedir nada más.

**Why this priority**: Mejora mucho la confianza del pasajero al elegir, pero la búsqueda es útil sin ello (la app puede mostrar la lista).

**Independent Test**: Buscar con ≥ 4 rides viables y verificar que exactamente las primeras K opciones traen trazado, que el resto trae trazado nulo, y que los puntos ajustados caen sobre una vía transitable.

**Acceptance Scenarios**:

1. **Given** 5 rides viables y K = 3, **When** el pasajero busca, **Then** los rides con rank 1–3 traen el trazado del recorrido y los rank 4–5 lo traen nulo.
2. **Given** un pickup marcado en medio de una manzana o un parque, **When** el pasajero busca, **Then** el punto de recogida devuelto es el punto transitable más cercano y el desvío se calcula desde ese punto ajustado.

---

### User Story 3 - Cada resultado mostrado queda registrado para aprender del comportamiento (Priority: P3)

Cada vez que el sistema devuelve resultados, publica un evento `match.shown` con lo que se mostró (qué rides, en qué posición, con qué puntuación, qué versión del modelo de ordenamiento se usó y las características usadas para puntuar), de modo que más adelante se pueda entrenar un ranker aprendido comparando lo mostrado con lo que el pasajero eligió.

**Why this priority**: Es la base para mejorar el ordenamiento con datos reales, pero no afecta la experiencia inmediata del pasajero.

**Independent Test**: Hacer una búsqueda con resultados y verificar que en el flujo de eventos aparece exactamente un `match.shown` correlacionable por `requestId`, con todos los candidatos mostrados.

**Acceptance Scenarios**:

1. **Given** una búsqueda con N > 0 resultados, **When** se responde al pasajero, **Then** se publica un `match.shown` con el `requestId`, el `passengerId`, la versión del modelo y los N candidatos con su rank, score, desvío y ETA.
2. **Given** que el bus de eventos no está disponible, **When** el pasajero busca, **Then** la respuesta al pasajero se entrega igual (la publicación del evento no bloquea ni hace fallar la búsqueda) y el fallo queda registrado en logs.

---

### User Story 4 - Errores claros cuando la búsqueda no puede atenderse (Priority: P2)

`crud-ts` necesita distinguir entre "no hay rides" (lista vacía), "la búsqueda es inválida" (p. ej. coordenadas fuera de la península) y "el servicio de rutas no está disponible", para mostrar el mensaje correcto al pasajero o reintentar.

**Why this priority**: Sin esta distinción, crud-ts mostraría "no hay viajes" cuando en realidad el sistema está caído, y el pasajero se iría.

**Independent Test**: Enviar coordenadas fuera del área, y simular caída del motor de rutas; verificar los códigos de respuesta.

**Acceptance Scenarios**:

1. **Given** un pickup o dropoff fuera del rectángulo geográfico de la península de Yucatán, **When** se busca, **Then** la respuesta es 422 con un mensaje que identifica el campo inválido.
2. **Given** que el motor de rutas no responde tras un reintento, **When** se busca, **Then** la respuesta es 503 (no una lista vacía).
3. **Given** un request con campos faltantes o mal tipados (p. ej. `departureAt` no ISO-8601), **When** se busca, **Then** la respuesta es 422.

---

### Edge Cases

- **Celdas sin ruta posible**: si el motor de rutas no encuentra camino entre el origen del conductor y el pickup, entre pickup y dropoff, o entre dropoff y el destino del conductor, ese ride se descarta (no se devuelve con desvío infinito ni se hace fallar toda la búsqueda).
- **Sin ruta entre pickup y dropoff del pasajero**: ningún ride puede ser viable → lista vacía.
- **Pickup y dropoff idénticos o muy cercanos**: se procesa normalmente; si todos los desvíos resultan triviales el orden depende del resto de factores de relevancia.
- **Preferencia `max_detour_minutes` del pasajero más estricta que la del conductor**: rige la del pasajero; si es 0, solo pasan rides con desvío ≤ 0.
- **Desvío negativo** (la ruta con pasajero resulta más corta que la ruta registrada del conductor por diferencias de ajuste o de grafo): se trata como desvío 0 a efectos de filtro y puntuación, conservando el valor real para diagnóstico. *(supuesto razonable)*
- **Más candidatos que el tamaño máximo de matriz del motor de rutas**: el cálculo se reparte en varios lotes de forma transparente; el resultado debe ser idéntico al de una sola consulta.
- **Más de 80 rides cerca del corredor**: solo se evalúan los 80 cuyo corredor queda más cerca de los puntos del pasajero.
- **`limit` mayor al número de viables**: se devuelven solo los viables.
- **`limit` o `timeWindowMin` ausentes**: se usan los defaults del contrato (10 y 30 min respectivamente).
- **Ride que se llena o se cancela entre la búsqueda y la respuesta**: aceptable; la reserva la valida crud-ts al confirmar.
- **El pasajero no tiene preferencias estructuradas** (fila inexistente o `structured` nulo): se busca sin aplicar preferencias.
- **Reintento de crud-ts con el mismo `requestId`**: se recalcula y responde normalmente; puede generar un segundo `match.shown` con el mismo `requestId` (los consumidores deben deduplicar por `requestId`).

## Requirements *(mandatory)*

### Functional Requirements

**Contrato de búsqueda (consumido por crud-ts, no se cambia sin versionar)**

- **FR-001**: El sistema DEBE exponer una operación de búsqueda `POST /match` en la red interna que reciba `requestId`, `passengerId`, `pickup {lat,lng}`, `dropoff {lat,lng}`, `departureAt` (ISO-8601), `timeWindowMin` (default 30) y `limit` (default 10), con el formato exacto definido en [input-technical-notes.md](./input-technical-notes.md#contrato-http).
- **FR-002**: La respuesta exitosa DEBE incluir `requestId` (eco del request), `modelVersion` (`heuristic-v1` en esta entrega) y `candidates`: cada uno con `rideId`, `rank`, `score`, `detourS`, `pickupEtaS`, `snappedPickup`, `snappedDropoff` y `routeGeometry`.
- **FR-003**: Una búsqueda sin rides viables DEBE responder éxito con `candidates: []`.
- **FR-004**: El sistema DEBE rechazar con 422 cualquier búsqueda cuyo pickup o dropoff esté fuera del rectángulo geográfico de la península de Yucatán, o cuyo cuerpo no cumpla el contrato.
- **FR-005**: El sistema DEBE responder 503 cuando el motor de rutas no esté disponible tras un reintento, en lugar de devolver una lista vacía.

**Selección de candidatos**

- **FR-006**: Solo son candidatos los rides en estado programado (`scheduled`), con al menos un asiento libre, cuya hora de salida esté dentro de ±`timeWindowMin` de `departureAt`, cuyo conductor no sea el propio pasajero y con los que el pasajero no tenga ya un match activo.
- **FR-007**: Un ride solo es candidato si su ruta pasa a no más de `CANDIDATE_RADIUS_M` (default 1500 m) **tanto** del pickup **como** del dropoff.
- **FR-008**: Un ride solo es candidato si recorre su ruta en el sentido del pasajero: el punto de la ruta más cercano al pickup debe estar antes que el más cercano al dropoff.
- **FR-009**: Se evalúan como máximo `MAX_CANDIDATES` (default 80) rides por búsqueda, priorizando los que tienen menor suma de distancias de la ruta al pickup y al dropoff.
- **FR-010**: La fuente de candidatos DEBE ser intercambiable (p. ej. pasar de búsqueda geoespacial directa a un índice hexagonal H3) sin modificar la lógica de la operación de búsqueda.

**Cálculo de desvío exacto**

- **FR-011**: Antes de calcular desvíos, pickup y dropoff DEBEN ajustarse al punto transitable más cercano del grafo vial; esos puntos ajustados son los que se devuelven y los que se usan en todos los cálculos.
- **FR-012**: Para cada candidato, el desvío DEBE calcularse como: tiempo(origen del conductor → pickup) + tiempo(pickup → dropoff) + tiempo(dropoff → destino del conductor) − duración original registrada del ride, usando tiempos de manejo reales del grafo vial de Yucatán.
- **FR-013**: Los tiempos de todos los candidatos DEBEN obtenerse en una sola consulta de matriz al motor de rutas por búsqueda (repartida en lotes solo cuando se exceda el tamaño máximo de matriz configurado, `OSRM_MAX_TABLE_SIZE`, default 100, que debe coincidir con el límite configurado en el motor).
- **FR-014**: Un candidato DEBE descartarse si alguno de los tres tramos no tiene ruta posible o si su desvío supera el desvío máximo aceptado por su conductor (`max_detour_s`).
- **FR-015**: `pickupEtaS` DEBE ser el tiempo de manejo desde el origen del conductor hasta el pickup ajustado. *(supuesto: el texto original se corta en "Guardar también"; se asume que lo que se guarda además del desvío es este tiempo y las distancias de cada tramo, para usarlos como características de ranking.)*

**Preferencias y ordenamiento**

- **FR-016**: El sistema DEBE leer las preferencias estructuradas del pasajero (`user_preferences.structured`, esquema `StructuredPreferences` v1 del slice preferences) y aplicarlas así:
  - **Filtro duro**: si `max_detour_minutes` no es nulo, se descarta todo candidato con `detourS > max_detour_minutes × 60` (además del límite `max_detour_s` del conductor; aplica el más restrictivo).
  - **`gender_preference`**: se ignora en esta entrega (los datos del conductor necesarios no están disponibles para este slice).
  - **Resto de preferencias** (`allow_pets`, `allow_smoking`, `allow_music`, `preferred_areas`, `avoided_areas`, `time_windows`, `avoid_strangers_at_night`): solo pueden ajustar el score, nunca excluir. Como los rides aún no exponen los atributos correspondientes, en `heuristic-v1` no modifican el score; se registran como características en `match.shown` para el ranker futuro.
- **FR-017**: El sistema DEBE asignar a cada candidato viable un `score` en [0, 1] con la heurística `heuristic-v1`:

  `score = 0.5 · (1 − detour_n) + 0.3 · (1 − time_n) + 0.2 · (1 − eta_n)`

  donde cada factor se recorta a [0, 1]:
  - `detour_n = max(detourS, 0) / límite de desvío efectivo` (el más restrictivo entre `max_detour_s` del conductor y `max_detour_minutes` del pasajero).
  - `time_n = |hora de salida del ride − departureAt| / timeWindowMin`.
  - `eta_n = pickupEtaS / PICKUP_ETA_NORM_S` (configurable, default 1800 s).

  Los pesos DEBEN ser configurables sin cambiar código, y la versión del modelo reportada sigue siendo `heuristic-v1` mientras no cambie la fórmula.
- **FR-018**: Los candidatos DEBEN ordenarse por `score` descendente (desempate determinista por menor desvío y luego por `rideId`), numerarse con `rank` desde 1 y recortarse a `limit`.
- **FR-019**: La estrategia de ordenamiento DEBE ser intercambiable (heurística ahora, modelo aprendido `lgbm-vN` después) y la respuesta DEBE reportar en `modelVersion` cuál se usó.

**Trazado**

- **FR-020**: Solo los primeros `ROUTE_GEOMETRY_TOP_K` (default 3) candidatos DEBEN incluir `routeGeometry`: el trazado completo origen del conductor → pickup → dropoff → destino del conductor como línea GeoJSON; el resto DEBE llevar `null`.

**Eventos**

- **FR-021**: Por cada búsqueda respondida con al menos un candidato, el sistema DEBE publicar un evento `match.shown` en `rides.events` siguiendo el mismo formato de envelope que usa crud-ts (`id, type, version, source, aggregateType, aggregateId, occurredAt, data`), con `source = matching-py`, y en `data`: `requestId`, `passengerId`, `modelVersion`, `departureAt` y la lista de candidatos mostrados con `rideId`, `rank`, `score`, `detourS`, `pickupEtaS` y las características usadas para puntuar. *(supuesto: no se emite evento para búsquedas sin resultados.)*
- **FR-022**: La publicación de `match.shown` NO DEBE retrasar perceptiblemente ni hacer fallar la respuesta; un fallo de publicación se registra y se descarta.

**Restricciones de datos**

- **FR-023**: Este slice DEBE tratar `rides`, `matches` y `user_preferences` como solo lectura; no escribe en ninguna tabla.
- **FR-023a**: La ruta original de cada ride (`route`, `duration_s`, `distance_m`) la crea y la mantiene crud-ts al publicar el ride. Este slice NO DEBE crear, recalcular ni persistir esa ruta. Solo calcula, al vuelo y sin guardarlo, el recorrido con el pasajero incluido (A→P→D→B) para obtener el desvío y el trazado.
- **FR-023b**: Este slice NO DEBE incluir migraciones que creen o alteren `rides`, `matches` ni `user_preferences`. Si las pruebas necesitan esas tablas, deben crearse con fixtures de prueba fuera de `migrations/`.
- **FR-024**: Este slice NO DEBE invocar al modelo de lenguaje (Bedrock) en ningún momento.

**Observabilidad**

- **FR-025**: Cada búsqueda DEBE registrar en logs estructurados (JSON, mismo formato que el resto del servicio) el `requestId`, número de candidatos generados, descartados por motivo (sin ruta, desvío excedido, preferencias) y devueltos, y el tiempo empleado en cada fase (candidatos, ajuste, matriz, trazado).

### Key Entities

- **Búsqueda de match (MatchRequest)**: intención de viaje de un pasajero — quién, desde dónde, hasta dónde, a qué hora, con qué tolerancia horaria y cuántos resultados quiere. Identificada por `requestId` generado por crud-ts.
- **Ride**: viaje publicado por un conductor (propiedad de crud-ts) — origen, destino, trazado de la ruta, duración y distancia originales, hora de salida, asientos libres, desvío máximo aceptado y estado.
- **Match**: relación existente entre pasajero y ride (propiedad de crud-ts); aquí solo se consulta para excluir rides con match activo.
- **Preferencias estructuradas**: preferencias del pasajero ya extraídas por el slice preferences (esquema v1).
- **Candidato**: ride viable para una búsqueda, con puntos ajustados, desvío, ETA al pickup, características de ranking, score, rank y, opcionalmente, trazado.
- **Evento `match.shown`**: registro inmutable de qué se mostró a quién, en qué orden y con qué modelo, para entrenamiento posterior.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: El 95 % de las búsquedas con 80 candidatos a evaluar se responden en menos de 400 ms medidos en el servicio.
- **SC-002**: En un conjunto de prueba de rides sembrados, el 100 % de los rides que cumplen todos los criterios aparecen y el 0 % de los que incumplen alguno (sentido contrario, sin asientos, desvío excedido, fuera de ventana, propio, con match activo) aparecen.
- **SC-003**: El desvío reportado difiere en menos de 1 segundo del calculado por una consulta de ruta completa independiente con los mismos puntos ajustados.
- **SC-004**: El 100 % de las búsquedas con resultados generan exactamente un evento `match.shown` correlacionable por `requestId` (salvo indisponibilidad del bus de eventos, que queda registrada).
- **SC-005**: Cuando el motor de rutas está caído, el 100 % de las búsquedas devuelven 503 y ninguna devuelve una lista vacía engañosa.
- **SC-006**: El resultado (candidatos, orden, desvíos) es idéntico se calcule en un lote o repartido en varios lotes.

## Assumptions

- `matching-py` hoy solo corre el consumer de Redis Streams; esta feature añade un servidor HTTP interno que debe convivir en el mismo servicio (o proceso) con el consumer. La spec Kiro de preferences declaraba "crear endpoints HTTP" fuera de alcance *de ese slice*; este slice lo introduce.
- El motor de rutas (OSRM, perfil `driving`, grafo de Yucatán) corre como contenedor aparte en la red interna (`OSRM_URL`) y ya está desplegado; este slice solo lo consume.
- La duración original del ride (`duration_s`) fue calculada con el mismo motor y grafo, por lo que es comparable con los tiempos de la matriz.
- "Match activo" = matches del pasajero con ese ride en estados no terminales (p. ej. pendiente o aceptado); los estados exactos se toman del esquema de crud-ts durante el plan.
- El rectángulo de la península de Yucatán se fija como configuración (aprox. lat 17.8–21.7, lng −92.5 a −86.7) y se afina en el plan.
- La llamada desde crud-ts es de confianza (red interna); no se añade autenticación en esta entrega.
- El evento `match.shown` se publica en el mismo stream `rides.events`; el consumer del slice preferences lo ignora (ACK sin procesar, RF-01 de la spec Kiro).
- El ranker aprendido (`lgbm-vN`) y su entrenamiento quedan fuera de alcance; solo se deja el punto de intercambio.
- Umbral de normalización de ETA (`PICKUP_ETA_NORM_S`) = 1800 s (30 min) por defecto; ETAs mayores aportan 0 en ese factor.
- `gender_preference=same_only` no se respeta en esta entrega; queda como trabajo pendiente cuando el género del conductor esté disponible para matching-py.
- El texto técnico de entrada llegó truncado tras "Guardar también"; cualquier requisito posterior (p. ej. secciones de ranking, evento o preferencias) se incorporará vía `/speckit-clarify`.
