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
| 9 | Manufacturing outputs (gerbers, drill, BOM, render, fab package) | ✅ completada |
| 10 | Design rules + net classes + auto-annotation + schematic↔PCB sync | ✅ completada |
| 11 | Copper zones + mounting holes + silk text + fiducials + BOM enriquecido | ✅ completada |
| 12 | Diff pairs + length tuning (meander) + buses de comunicación | ✅ completada |
| 13 | STEP 3D + custom DRC rules + multi-board + symbol/footprint editor | ✅ completada |

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

## Fase 9 — checklist

- [x] `kicad_cli.py` extendido: `export_gerbers`, `export_drill`, `export_pos`, `export_bom`, `export_netlist`, `render_pcb`, `export_pcb_svg`. Helper `_run` reusable + `_list_files` para diff de output dir.
- [x] `tools/manufacturing.py`: 8 tools (`export_gerbers`, `export_drill`, `export_pos`, `export_bom`, `export_netlist`, `render_pcb_3d`, `export_pcb_svg`, `export_fab_package`).
- [x] **`export_fab_package`** — one-shot bundle: gerbers + drill + pos + BOM + render opcional, todo bajo `<project>/fab/` listo para zipear.
- [x] Defaults sensatos: `<project>/fab/gerbers/`, `<project>/fab/drill/`, `<project>/fab/<name>-pos.csv`, etc. — el usuario no necesita pensar en paths.
- [x] Validación de inputs en el adapter (side, format, etc.) → KicadCliError con mensajes claros.
- [x] Tests: 5 unit + 8 acceptance (kicad-cli real). Render PNG produce >1KB, BOM CSV con headers, gerbers per-layer, drill PTH/NPTH separados.

## Decisiones técnicas (Fase 9)

- **PNG render por defecto** (transparente para PNG, opaco para JPEG según `--background` default de KiCAD). Quality `basic` por defecto: una placa pequeña ~1-2s. `high` puede tardar 30s+ pero queda bonito.
- **Drill PTH/NPTH separados por defecto** (`--excellon-separate-th`). Es lo que pide JLCPCB y la mayoría de fabs.
- **Drill map en PDF por defecto** (configurable a SVG/DXF/PS). El PDF es el más universal.
- **POS en CSV con mm** por defecto (CSV facilita pick-and-place automatizado en JLCPCB/PCBWay).
- **`export_fab_package` no aborta en errores parciales**: si el BOM falla por esquema vacío, sigue con el resto. Cada step retorna su propio dict (con `error` si falló) para que el caller decida.

## Fase 10 — checklist

### Design rules

- [x] `adapters/project_settings.py`: load/save `.kicad_pro` JSON con helpers para `rules`, `classes`, `netclass_patterns`
- [x] **5 fab presets**: `jlcpcb_2l_default`, `jlcpcb_2l_advanced`, `pcbway_default`, `oshpark_4l`, `permissive_prototype`
- [x] **JSON keys correctos descubiertos** llamando `pcbnew.GetDesignSettings.SetX(FromMM(...))` y guardando: `min_clearance`, `min_track_width`, `min_via_diameter`, `min_via_drill`, `min_through_hole_diameter`, `min_hole_clearance`, `min_hole_to_hole`, `min_silk_clearance`, `min_text_height`, `min_text_thickness`, `min_copper_edge_clearance`, `allow_blind_buried_vias`, `allow_microvias`. Verificado: DRC sí los enforza (`Anchura de pista` violation con `min_track_width=2.0`).

### Net classes

- [x] `add_net_class(name, track_width, clearance, via_diameter, via_drill, diff_pair_width, diff_pair_gap)` — crea o actualiza
- [x] `assign_net_class(net_pattern, class_name)` — pattern matching tipo glob (KiCAD)
- [x] `list_net_classes` lista clases + sus patterns
- [x] `remove_net_class` también elimina patterns colgantes

### Annotation

- [x] `adapters/annotation.py`: detecta `R?`/`U?`/etc., respeta refs ya numeradas (continúa el numeral), sort por posición top-down/left-right
- [x] **Multi-sheet aware**: pasa el contador entre hojas, no se solapa entre root/children
- [x] Actualiza `(reference ...)` dentro de `instances` block (necesario para que KiCAD lo reconozca)
- [x] **Bug fix colateral**: `add_symbol` ahora permite refs con `?` duplicadas (se resuelven luego con annotate)

### Schematic↔PCB sync

- [x] `adapters/kicad_python.py`: extendido con `apply_netlist` que invoca `pcbnew.LoadBoard` + `FindNet` + `pad.SetNet` desde el Python bundled
- [x] `tools/sync.py`:
  - `annotate_schematic` (multi-sheet)
  - `update_pcb_from_schematic` exporta netlist como `kicadxml` y lo aplica al .kicad_pcb
- [x] **Acceptance E2E**: divisor con `R?, R?` → annotate → JLCPCB preset → footprints → update_pcb_from_schematic → autoroute → **Freerouting realmente rutea 1 segmento** (R1.pad2 ↔ R2.pad1, F.Cu, 0.2mm)

## Decisiones técnicas (Fase 10)

- **JSON keys verificadas empíricamente.** El probe inicial (escribir vía pcbnew Python y leer el `.kicad_pro`) reveló los nombres exactos. Sin esto habría sido fácil escribir `minClearance` (camelCase) o `min_clearance_mm` y que KiCAD no leyera nada.
- **`apply_netlist` usa `kicadxml`** (no kicadsexpr) porque XML es más fácil de parsear con stdlib (`xml.etree`), y los nodos `<node ref pin>` son directos.
- **`board.FindNet(name)` para existencia** — el SWIG `NETNAMES_MAP` no es un dict de Python; `FindNet(name)` retorna `None` o el `NETINFO_ITEM`, lo que sí funciona con el patrón `if … is None`.
- **`update_pcb_from_schematic` no añade footprints automáticamente.** Reporta `missing_in_pcb`; el caller usa `add_footprint` para los que falten y vuelve a llamar. Trade-off: más explícito; coste: dos pasos. Se podría unificar si lo necesitas.
- **Annotation hace sort por posición KiCAD-Y** (ascendente = top-down). KiCAD GUI usa el mismo criterio.

## Fase 11 — checklist

- [x] `pcb_editor.add_zone(net_name, layer, polygon_mcp, ...)`: emit `(zone ...)` con polígono, fill yes, thermal relief default
- [x] `pcb_editor.add_ground_plane(layer="B.Cu")`: extrae el outline del board (gr_rect Edge.Cuts) y crea zona GND
- [x] `pcb_editor.add_silk_text(text, layer)`: F.SilkS / B.SilkS / F.Fab / B.Fab / F.Cu / B.Cu
- [x] Tool MCP `add_zone`, `add_ground_plane`, `add_silk_text`
- [x] Tool MCP `add_mounting_hole(diameter_mm, plated)`: **resolve por búsqueda en index**, no asume nombres exactos. Auto-numera H1, H2, ...
- [x] Tool MCP `add_fiducial(size, layer)`: 0.5mm/0.75mm/1mm/1.5mm. Auto-numera FID1, FID2, ...
- [x] `run_drc(refill_zones=True)`: pasa `--refill-zones --save-board` para que los polígonos se computen antes de validar
- [x] `enrich_bom_with_sourcing`: lee BOM de KiCAD, hits DigiKey + Mouser por `Value` (configurable), append columnas `dk_*` / `mo_*`. Cache por query (no re-hits).
- [x] Tests: 15 unit + 1 slow acceptance + 1 live network. Acceptance valida tablero 80×60 con outline + 2 R + 4 mounting holes M3 + silk text + fiducial + GND plane → DRC `0 errors`. Live BOM: enriquecimiento real con DigiKey hit en LM358N.

## Decisiones técnicas (Fase 11)

- **GND plane = polígono = outline del board.** Más simple que recortes manuales. KiCAD respeta automáticamente `min_copper_edge_clearance` al rellenar.
- **`add_mounting_hole` por búsqueda en el index.** El usuario pasa el diámetro de drill y el flag `plated`; nosotros buscamos `MountingHole_<d>mm*` y elegimos la variante simpler (`min(candidates, key=len)`). Robusto a las variaciones de naming en KiCAD libs.
- **DRC con `--refill-zones --save-board`.** Sin `--save-board`, los polígonos rellenados solo viven en memoria durante DRC; con él, quedan en el archivo (mejor para visualización + handoff).
- **BOM enrichment usa cache por query.** Si tres líneas del BOM tienen `Value="10k"`, solo una llamada a DigiKey y una a Mouser. Reduce rate-limiting drásticamente.
- **`sourcing_field` configurable.** Por defecto "Value" (lo que KiCAD escribe sin más config), pero el usuario puede pasar "MPN" si su esquema tiene ese campo custom.
- **Errores parciales surfaced.** Si Mouser falla auth, las columnas `mo_*` quedan vacías y el `errors` dict del resultado lo refleja — el resto del BOM sigue enriquecido.

## Fase 12 — checklist

### Differential pair routing

- [x] `pcb_editor.find_diff_pair_candidates`: detecta pares por sufijos `_P/_N`, `+/-`, `DP/DM`. Strip de separador final del base name (USB_DP+USB_DM → "USB").
- [x] `tool list_diff_pair_candidates` lo expone
- [x] `tool add_diff_pair_class(name, diff_pair_width, diff_pair_gap, ...)` — wrapper sobre `add_net_class` con campos diff_pair llenos. Tip incluido en respuesta sobre naming + assign_net_class.

### Length tuning

- [x] `adapters/length_tuning.py`: generador puro-Python de patrón triangular. Auto-ajusta amplitud efectiva para hit del target exacto (resuelve `2·hypot(b/2, A) − b = ΔL/n_bumps` para `A`).
- [x] `tool compute_trace_length(net_name)`: suma segmentos por net, breakdown por capa
- [x] `tool validate_diff_pair_length_match(p, n, tolerance)` — reporta skew + cuál es el más largo
- [x] `tool add_meander(x1, y1, x2, y2, target_length, amplitude, side, layer, net)`: emite chain de `(segment ...)`, side="up"/"down" perpendicular, hits target con precisión <0.001mm
- [x] Default `base_width = amplitude` (extras 1.236× per base — empaqueta más en menos espacio que el inicial 2A)

### Buses

- [x] `sch_editor.add_bus_segment(x1, y1, x2, y2)` → `(bus ...)` con stroke
- [x] `sch_editor.add_bus_entry(x, y, direction)`: 4 direcciones (right_down/right_up/left_down/left_up), size=2.54×2.54 default
- [x] `sch_editor.add_bus_alias(name, members)` → `(bus_alias "DATA" (members "D0" "D1" ...))`
- [x] Tools MCP equivalentes (`add_bus`, `add_bus_entry`, `add_bus_alias`)
- [x] Buses respetan `active_sheet` (multi-sheet aware)
- [x] **Acceptance**: tablero con USB diff pair detectado, USB net class con dp_width=0.2/dp_gap=0.18, meander 55mm exacto, bus + alias DATA[0..7] en esquema, kicad-cli sch erc + pcb drc returncode 0.

## Decisiones técnicas (Fase 12)

- **Diff pair coupling vía Freerouting transparente**: KiCAD's Specctra DSN exporter ya emite `(class diff_pair ...)` automáticamente cuando una netclass tiene `diff_pair_width` y `diff_pair_gap` > 0. Nuestro trabajo es solo asegurar esos campos en la netclass + naming consistente.
- **Detection patterns con strip de separator**: `USB_DP` matches "DP" suffix, base "USB_" → strip → "USB" para display. Permite tanto `USB_DP/USB_DM` como `USBDP/USBDM`.
- **Meander auto-ajusta amplitud efectiva**: con `n_bumps` redondeado al entero superior, hay overshoot. Resolvemos para A_eff que hit el target exacto con esos n_bumps. Así achieved_length ≈ target con precisión <0.001mm.
- **Triangular wave > rectangular**: KiCAD GUI usa rectangular con esquinas a 45°, pero triangular es más simple de generar y KiCAD lo acepta. El usuario puede afinar en GUI.
- **Buses son visuales**: las conexiones eléctricas reales se hacen vía labels en wires individuales. El `bus_alias` solo permite escribir "DATA" en vez de "DATA[0..7]" en la línea, y KiCAD expande a los miembros declarados.
- **`add_meander` no modifica trazas existentes**: el usuario borra el segmento recto en la GUI (o programáticamente con un futuro tool) y añade el meander entre los mismos endpoints. Trade-off: más explícito y reversible, sin tocar nada que no haya pedido.

## Fase 13 — checklist

### 3D STEP export
- [x] `kicad_cli.export_step`: wrap completo con flags `--include-tracks/--include-zones/--include-silkscreen/--include-soldermask/--no-dnp/--component-filter`
- [x] `tool export_step_3d` con defaults sensatos (board + components, basic quality)

### Custom DRC rules
- [x] `adapters/drc_rules.py`: read/write `.kicad_dru` (sexpdata para parse, text generation para write). Validación de constraint_type y severity.
- [x] 4 tools: `add_drc_rule`, `list_drc_rules`, `remove_drc_rule`, `clear_drc_rules`
- [x] Constraint types soportados: clearance, hole_clearance, silk_clearance, edge_clearance, courtyard_clearance, physical_clearance, track_width, via_diameter, via_drill, hole_size, diff_pair_gap, diff_pair_uncoupled, length, skew, text_height, annular_width, disallow, etc.
- [x] **Acceptance**: regla `track_width >= 1mm` dispara violation contra una pista de 0.25mm.

### Multi-board management
- [x] `state.set_active_board / get_active_board_path`: tracking del PCB activo
- [x] 3 tools: `add_board(name)` crea `.kicad_pcb` + registra en `.kicad_pro`'s `boards[]`, `list_boards`, `set_active_board`
- [x] **Refactor cross-cutting**: `tools/pcb`, `tools/routing`, `tools/sync`, `tools/validation`, `tools/manufacturing` ahora usan `state.get_active_board_path()` en lugar de `proj.pcb_path`. Permite que TODO el flujo (autoroute, DRC, gerbers, etc.) trabaje con el board activo.
- [x] State reset al cambiar proyecto (active_board → None)

### Symbol/footprint editor
- [x] `adapters/library_create.py`: `build_symbol_node` (rectangular body + pins) y `build_footprint_node` (pads + auto-courtyard + auto-silk-outline)
- [x] Validación de pin_type (input/output/passive/power_in/...) y pad_type (smd/thru_hole/np_thru_hole) y shapes
- [x] Auto-drill para THT pads: `max(0.3, min(sx, sy) - 0.4)` para anillo annular sensato
- [x] 2 tools: `create_symbol(lib_name, symbol_name, pins, body_size, properties)`, `create_footprint(lib_name, footprint_name, pads, description, tags)`
- [x] Reusa `vendor_import.update_sym_lib_table` / `update_fp_lib_table` para registrar libs en el proyecto
- [x] **Acceptance**: opamp custom 5-pin + SOIC-8 SMD 8-pad → kicad-cli sch erc + pcb drc returncode 0.

## Decisiones técnicas (Fase 13)

- **Multi-board cross-cutting refactor**: añadir `active_board` requirió actualizar `proj.pcb_path` → `state.get_active_board_path()` en 5 archivos de tools. Aceptable trade-off por consistencia. Sin esto, autoroute/DRC/manufacturing ignoraban el board activo y operaban siempre sobre el main.
- **`.kicad_dru` es text-level write**: escribimos rules como texto formateado (template strings), no via sexpdata. Reading sí usa sexpdata para tolerar archivos creados a mano. Más simple, menos fragility.
- **`sexpdata.Symbol` no es `==` a string**: aprendido por dolor. `Symbol("0.5mm").__eq__("0.5mm")` es False porque Symbol's `__eq__` solo matches Symbol→Symbol. Tuvimos que castear explícitamente en `_parse_dim`.
- **Footprint creator auto-courtyard**: el F.CrtYd line outline alrededor del pad bounding box, inflado 0.25mm. Es lo que el DRC espera por defecto. Disable con `add_courtyard=False` si la app lo necesita.
- **Symbol creator usa rectangular body**: 95% de los símbolos custom (ICs, conectores) caben en una caja con pines en los bordes. Para shapes complex (transistores con triángulos, transformadores, etc.) el usuario edita el `.kicad_sym` directamente o usa la GUI.
- **Multi-board no es panelización**: este Phase 13 es para "proyectos con varios PCBs distintos" (main + breakout, etc), no para duplicar el mismo board en un panel. Panelización es problema diferente; herramientas como KiKit son más apropiadas.

## Resumen del proyecto

**84 tools MCP** registrados. **171 tests rápidos + 30 acceptance**. 14 commits limpios.

## Notas

Cada fase termina con un commit `feat(phase-N): <descripción>` y una pausa
para que el usuario revise antes de avanzar (§14 del spec).

KiCAD instalado en este equipo: **10.0.1** (el spec asume 9.0+; las
versiones de formato usadas son `.kicad_sch=20250114`, `.kicad_pcb=20241229`,
`.kicad_pro meta.version=3`, validadas con `kicad-cli` v10).
