# Progreso por fase

| Fase | Descripción | Estado |
|------|-------------|--------|
| 0 | Bootstrap (estructura, deps, ping tool) | ✅ completada (pendiente verificación end-to-end con Claude Code) |
| 1 | Gestión de proyecto KiCAD | ✅ completada |
| 2 | Indexador de librerías | ✅ completada |
| 3 | Edición del esquema | ✅ completada |
| 4 | Sourcing externo (DigiKey/Mouser/SnapEDA) | ✅ completada (Mouser pendiente de Search API key correcta) |
| 5 | Edición del PCB | ✅ completada |
| 6 | Autorouting con Freerouting | ✅ completada |
| 7 | Validación (ERC/DRC) | ✅ completada |
| 8 | Hierarchical sheets + multi-layer PCBs (extra-spec) | ✅ completada |

## Fase 0 — checklist

- [x] Estructura de carpetas según §4 del spec
- [x] `pyproject.toml` con `mcp[cli]>=1.25,<2`, `kicad-skip>=0.2.5`, `httpx`, `pydantic>=2`
- [x] `server.py` con tool `ping() -> "pong"`
- [x] `.env.example` con variables del spec
- [x] Logger configurado a stderr (`utils/logging.py`)
- [x] README con instrucciones de instalación y configuración en Claude Code
- [x] `.gitignore` y `git init`
- [x] `uv sync` ejecutado correctamente
- [x] Smoke test: `from server import ping; ping()` devuelve `"pong"`
- [x] `pytest` pasa (`tests/test_ping.py`)
- [x] `uv run mcp --help` lista el subcomando `dev`
- [ ] Verificación end-to-end desde Claude Code (`/mcp` → llamar a `ping`) — paso manual del usuario

## Fase 1 — checklist

- [x] Plantilla blanca generada programáticamente (`templates/blank/` + `templates/blank.py`)
- [x] `state.py` con singleton `ActiveProject` (paths + validación)
- [x] Tool `create_project(path, name)` → crea `.kicad_pro` + `.kicad_sch` + `.kicad_pcb` + activa
- [x] Tool `set_project(project_path)` → acepta directorio o archivo `.kicad_*`
- [x] Tool `get_project_state()` → cuenta símbolos, footprints, nets
- [x] Tool `list_components()` → lista símbolos del esquema con reference/value/lib_id/posición
- [x] Tests: 13 pasan (`tests/test_phase1_project.py`)
- [x] Validación cruzada con `kicad-cli sch erc` y `kicad-cli pcb drc` sobre archivos generados
- [x] Imports estandarizados a `kicad_claude.*` (sin prefijo `src.`)

## Fase 2 — checklist

- [x] `utils/kicad_paths.py`: detecta `symbols/` y `footprints/` por OS (Darwin/Linux/Windows) + env vars `KICAD{N}_SYMBOL_DIR`
- [x] `indexer/kicad_libs.py`: parser S-expression con `sexpdata` para `.kicad_sym`, regex liviana para `.kicad_mod`. Resuelve `extends` para inherit de pin_count.
- [x] `indexer/search.py`: rapidfuzz `WRatio` + `default_process` (case-insensitive, alphanum-only). Cutoff 50 por defecto.
- [x] `tools/library.py`: 5 tools (`index_libraries`, `list_libraries`, `search_symbol`, `search_footprint`, `get_symbol_details`)
- [x] Memo en proceso del índice (no relee la JSON ~10MB en cada llamada)
- [x] Cache JSON en `~/.cache/kicad-claude/index.json`
- [x] Tests: 29 rápidos + 1 lento (real KiCAD install). Todos pasan.
- [x] Acceptance criteria sobre KiCAD 10.0.1: **222** libs / **22,728** símbolos, **155** libs / **15,430** footprints, ESP32-S3-WROOM-1 → 41 pins (idéntico al spec).

## Fase 3 — checklist

- [x] `utils/geometry.py`: `mcp_to_kicad_xy` (Y-flip), `normalize_rotation` (0/90/180/270 only), `rotate_xy`, `round_mm`
- [x] `adapters/sch_io.py`: parse + pretty-print s-expr al estilo KiCAD (round-trip de Arduino_Mega.kicad_sch verificado, kicad-cli returncode 0)
- [x] `adapters/sch_editor.py`: backups en `.backups/`, lib_symbols injection idempotente, instancia con UUID, project path, propiedades, pin uuids
- [x] `tools/schematic.py`: 9 tools (add_symbol, remove_symbol, move_symbol, add_wire, add_label, add_power_symbol, add_no_connect, list_pins, get_pin_position)
- [x] Auto-numerado `#PWR####` para `add_power_symbol`
- [x] Tests: 17 rápidos + 1 de aceptación. Total 46 rápidos en todo el proyecto.
- [x] **Acceptance**: divisor de tensión (R1=10k, R2=1k entre +5V y GND, 3 wires) → `kicad-cli sch erc` returncode 0.

## Decisiones técnicas

- **Y axis**: MCP API Y+ arriba, archivo KiCAD Y+ abajo. Conversión en `geometry.mcp_to_kicad_xy`. Page height A4 landscape = 210mm.
- **Sin `kicad-skip` para escribir**: en su lugar, parse con `sexpdata` + pretty-printer custom (`sch_io.dumps`). KiCAD acepta nuestra salida (returncode 0 en erc/drc). kicad-skip queda para reads donde es conveniente (Phase 1's list_components).
- **lib_symbols injection**: idempotente por lib_id. Cada `add_symbol` que use un nuevo `lib_id` añade su definición completa al bloque `(lib_symbols ...)`. Reusos no duplican.
- **Pin position math**: lib coords (Y down) rotadas por símbolo's rotation, luego desplazadas al símbolo origin, luego flip Y para MCP. Verificado con simetría (pin1 + pin2 = 2*center_y).
- **Backups**: cada escritura crea `<project>/.backups/<timestamp>_<filename>`.
- **`extends`**: el parser del indexador resuelve pin_count, pero `add_symbol` aún no inyecta la base extendida en lib_symbols. Símbolos como `Device:R_Small` (que extends `R`) pueden no renderizar bien hasta que esto se aborde. Pendiente para iteración.

## Fase 4 — checklist

- [x] `.env` cargado al arranque del server (`load_dotenv` antes de los registros)
- [x] `adapters/digikey.py`: V4 API. OAuth2 client_credentials, token cacheado en `~/.cache/kicad-claude/digikey_token.json` con expiry. Localización configurable (default ES/EUR).
- [x] `adapters/mouser.py`: V2 API, `apiKey` por query string. Maneja errores en payload (200 + Errors[]).
- [x] `adapters/snapeda.py`: helpers de URL + mensaje de fallback manual. Sin scraping (login required).
- [x] `adapters/vendor_import.py`: extrae ZIP, fusiona `.kicad_sym` (idempotente por nombre) y `.pretty/`, actualiza `sym-lib-table` y `fp-lib-table` (idempotente por lib name).
- [x] `tools/sourcing.py`: 4 tools (`check_availability`, `find_or_fetch_symbol`, `import_vendor_zip`, `list_vendor_parts`).
- [x] Tests: 13 unit + 2 network. Live DigiKey contra LM358N OK (29.540 stock, 0,87€).
- [x] `find_or_fetch_symbol` enriquece con manufacturer desde DigiKey si no hay match local.
- [x] `import_vendor_zip` rechaza `target_lib` no-alphanum (evita corrupción de lib-table).

### Mouser — atención

La API key actual devuelve `Invalid API Key`. Mouser entrega **dos claves
separadas por cuenta**: una para Search API (`/search/*`) y otra para Order
API (`/order/*`). La que funciona aquí es la de **Search**. Verificar en
https://www.mouser.com/api-hub/ → My Account → "Search API" key. Una vez
sustituida en `.env`, `check_availability` devolverá ambos lados.

## Fase 5 — checklist

- [x] `adapters/pcb_editor.py`: parse/save (reusa `sch_io`), iter_footprints, find_footprint_by_reference
- [x] `set_board_outline(width, height, shape='rect')` con `gr_rect` en Edge.Cuts. Idempotente (limpia outline previo).
- [x] `add_footprint(lib_id, ref, value, x, y, rotation, layer)` — clona def del `.kicad_mod`, le pone uuid/at/layer/properties. **Extiende el spec**: el spec asume "Update PCB from Schematic" en GUI; este tool permite poblar PCBs sin GUI (necesario para tests automáticos).
- [x] `move_footprint`, `remove_footprint`, `list_footprints_summary`
- [x] `place_footprints_grid` detecta footprints en KiCAD-(0,0) (estado tras "Update PCB from Schematic") y los reparte en rejilla. Sort por reference.
- [x] `add_track` (segment) y `add_via`
- [x] `tools/pcb.py`: 7 tools FastMCP
- [x] Tests: 13 unit + 1 acceptance. **72 pasan en total**.
- [x] **Acceptance**: tablero 50×30 mm + 2× R_0603 SMD + 1 track → `kicad-cli pcb drc` returncode 0.

## Decisiones técnicas (Fase 5)

- **Coords MCP Y+ arriba (mismo convenio que Fase 3).** Page height = 210 (A4 landscape) para el flip Y. La placa por defecto se ancla con esquina inferior-izquierda en MCP (10, 10).
- **`add_footprint` añadido fuera de spec.** Sin él no podríamos popular PCBs en tests sin GUI. Se mantiene compatible con el flujo del spec: si el usuario hace "Update PCB from Schematic" en KiCAD, los footprints aparecen y `place_footprints_grid` los puede ordenar.
- **`gr_rect` para outline rectangular.** La spec dice 'rect' o 'rounded_rect'; rounded_rect requiere 4 lines + 4 arcs y se aplazó (no bloquea el flujo).
- **Layer validation strict.** `add_footprint` y `move_footprint` solo aceptan `F.Cu` o `B.Cu`. Tracks aceptan cualquier capa cobre.

## Fase 6 — checklist

- [x] Freerouting 2.1.0 descargado a `third_party/freerouting.jar` (66 MB; gitignored)
- [x] `adapters/kicad_python.py`: localiza el Python 3.9 que trae KiCAD y ejecuta scripts con `pcbnew.ExportSpecctraDSN` / `ImportSpecctraSES`
- [x] `adapters/freerouting.py`: lanza el JAR vía subprocess con timeout duro; parsea logs (vias, longitud, % completion, duración)
- [x] `tools/routing.py`: 3 tools (`autoroute_pcb`, `export_dsn`, `import_ses`)
- [x] Backup del `.kicad_pcb` antes de importar SES
- [x] Tests: 5 unit + 2 acceptance (slow). 77 pasan en total.
- [x] **Acceptance**: pipeline completo en placa 50×30 + 2 R_0603 → DSN exportado, Freerouting OK (rc=0), SES importado, `.kicad_pcb` actualizado.

## Decisiones técnicas (Fase 6)

- **`kicad-cli` v10 NO expone Specctra**: el spec asumía `kicad-cli pcb export specctra-dsn` y `pcb import specctra-ses`, pero KiCAD 10 los retiró del CLI. **Workaround**: invocar `pcbnew.ExportSpecctraDSN` / `ImportSpecctraSES` desde el **Python 3.9 que KiCAD trae bundled**. La SWIG bindings sólo cargan en su propio intérprete (no en el `python` 3.10/3.13 nuestro).
- **Freerouting 2.2.x requiere Java 25**, así que descargué la **2.1.0** (compatible con Java 21+). Java 24 OK.
- **Path resolution para `freerouting.jar`**: 1) env `FREEROUTING_JAR`, 2) `<repo>/third_party/freerouting.jar`. Mismo patrón para `KICAD_PYTHON` y `JAVA_BIN`.
- **Stats parsing best-effort**: regex sobre stdout/stderr de Freerouting. Para campos que avanzan (passes_done, completion_pct) tomamos el último match (estado final). Para totales (vias, longitud) el primero/único.
- **Timeout duro** en `freerouting.route`: si supera `timeout_seconds` se aborta con error claro (sin esto Freerouting puede correr indefinidamente en boards patológicos).

## Fase 7 — checklist

- [x] `adapters/kicad_cli.py`: wrapper de `kicad-cli sch erc --format json` y `pcb drc --format json`. Parser estructurado: counts por severidad (`error`, `warning`, `exclusion`), violations con posición, raw JSON path para inspección humana.
- [x] DRC opcionalmente `--schematic-parity` y `--all-track-errors`.
- [x] `tools/validation.py`: 2 tools (`run_erc`, `run_drc`).
- [x] Tests: 3 unit (sobre payload sintético) + 3 acceptance (kicad-cli real).
- [x] **Acceptance**: ERC y DRC ejecutan limpios sobre divisor de tensión, JSON parseado, conteos por severidad correctos.

## Decisiones técnicas (Fase 7)

- **`kicad-cli` v10 sí expone JSON estructurado** vía `--format json`. Schemas oficiales de KiCAD: `schemas.kicad.org/erc.v1.json` y `schemas.kicad.org/drc.v1.json`.
- **DRC tiene 3 buckets distintos** que conviene separar al cliente: `violations` (clearance, etc.), `unconnected_items` (nets sin rutear), `schematic_parity` (mismatches con esquema). Cada uno se devuelve por separado además del total.
- **Severidad por defecto: `all`** (errors + warnings + exclusions). El usuario puede filtrar pasando `severity="error"` solo.
- **Raw JSON path expuesto** en cada respuesta (`raw_path`) por si quiere inspección a mano o re-parse.

## Fase 8 — checklist (extensiones extra-spec)

### PCBs hasta 12 capas (en realidad hasta 32)

- [x] `adapters/pcb_layers.py`: helpers `copper_layer_names(n)`, `copper_layer_id(name)`, `build_layers_block(n)`, `build_stackup_block(n)`
- [x] **Patrón de IDs descubierto empíricamente** (vía `pcbnew.SetCopperLayerCount` + inspección): F.Cu=0, B.Cu=2 (constante), In_k.Cu = 2*(k+1)
- [x] `pcb_editor.set_copper_layer_count(n)` reemplaza `(layers ...)` y `(setup (stackup ...) ...)` enteros
- [x] Stackup auto-genera dieléctricos con grosor uniforme para placa total ≈ 1.6 mm
- [x] Tool MCP `set_layer_count(n)` (n par, 2-32)
- [x] **Acceptance**: PCB de 12 capas con tracks en F.Cu, In3.Cu, In7.Cu, B.Cu → `kicad-cli pcb drc` returncode 0, sin violaciones de layer-undefined.

### Hierarchical sheets

- [x] `state.py`: añade `active_sheet_filename` + helpers `set_active_sheet`, `get_active_sheet_path`
- [x] `sch_editor`: `instance_path` parameter (en lugar de `schematic_uuid` solo) — soporta paths jerárquicos `/<root>/<child>`
- [x] `sch_editor.add_sheet_node`: `(sheet ...)` placeholder en root con UUID, properties Sheetname/Sheetfile, instances chain
- [x] `sch_editor.add_hierarchical_label`: shapes input/output/bidirectional/tri_state/passive
- [x] `sch_editor.add_sheet_pin`: pin en el sheet placeholder, matched-by-name con label en el child
- [x] `templates.write_blank_schematic`: helper para crear child sheets blancos (UUID propio)
- [x] 5 nuevos tools MCP: `add_sheet`, `set_active_sheet`, `get_active_sheet`, `list_sheets`, `add_hierarchical_label`, `add_sheet_pin` (6 en total)
- [x] **Refactor sin regresiones**: 80 tests previos siguen verdes tras cambiar `instance_path`
- [x] **Acceptance**: jerarquía 2 niveles (root → PSU sheet + Periph sheet), símbolos en cada child con instance path correcto, sheet pins en root → `kicad-cli sch erc` returncode 0.

## Decisiones técnicas (Fase 8)

- **Limitación a múltiplos de 2 en capas.** PCB profesional siempre es par (estructura sandwich). KiCAD acepta impares pero rara vez tiene sentido.
- **`set_layer_count` debe ir antes de `add_track`/`add_via`** sobre capas internas: items en capas que dejan de existir tras un cambio quedan huérfanos en el archivo (con warnings de DRC). Documentado en el docstring del tool.
- **`B.Cu = 2` es constante**, no shift. El instinct de "ID secuencial 0..N-1" era equivocado. La inspección directa de `pcbnew.SaveBoard` reveló el patrón real.
- **Sheet pins matched by name.** KiCAD asocia pins de un sheet placeholder en el padre con `hierarchical_label` del mismo nombre en el child. La posición geométrica del pin es decorativa; lo que importa es que coincida el nombre.
- **`active_sheet` global** (no por proyecto). Switching de proyecto resetea a root automáticamente. Trade-off: más sencillo de razonar; coste: si abres dos proyectos en paralelo en el mismo proceso, el estado se interfiere.

## Resumen del proyecto

**42 tools MCP** registrados. **93 tests rápidos + 14 acceptance**. 9 commits limpios.

## Notas

Cada fase termina con un commit `feat(phase-N): <descripción>` y una pausa
para que el usuario revise antes de avanzar (§14 del spec).

KiCAD instalado en este equipo: **10.0.1** (el spec asume 9.0+; las
versiones de formato usadas son `.kicad_sch=20250114`, `.kicad_pcb=20241229`,
`.kicad_pro meta.version=3`, validadas con `kicad-cli` v10).
