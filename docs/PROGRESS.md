# Progreso por fase

| Fase | Descripción | Estado |
|------|-------------|--------|
| 0 | Bootstrap (estructura, deps, ping tool) | ✅ completada (pendiente verificación end-to-end con Claude Code) |
| 1 | Gestión de proyecto KiCAD | ✅ completada |
| 2 | Indexador de librerías | ✅ completada |
| 3 | Edición del esquema | ✅ completada |
| 4 | Sourcing externo (DigiKey/Mouser/SnapEDA) | ⬜ pendiente |
| 5 | Edición del PCB | ⬜ pendiente |
| 6 | Autorouting con Freerouting | ⬜ pendiente |
| 7 | Validación (ERC/DRC) | ⬜ pendiente |

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

## Notas

Cada fase termina con un commit `feat(phase-N): <descripción>` y una pausa
para que el usuario revise antes de avanzar (§14 del spec).

KiCAD instalado en este equipo: **10.0.1** (el spec asume 9.0+; las
versiones de formato usadas son `.kicad_sch=20250114`, `.kicad_pcb=20241229`,
`.kicad_pro meta.version=3`, validadas con `kicad-cli` v10).
