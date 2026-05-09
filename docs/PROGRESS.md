# Progreso por fase

| Fase | Descripción | Estado |
|------|-------------|--------|
| 0 | Bootstrap (estructura, deps, ping tool) | ✅ completada (pendiente verificación end-to-end con Claude Code) |
| 1 | Gestión de proyecto KiCAD | ⬜ pendiente |
| 2 | Indexador de librerías | ⬜ pendiente |
| 3 | Edición del esquema | ⬜ pendiente |
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

## Notas

Cada fase termina con un commit `feat(phase-N): <descripción>` y una pausa
para que el usuario revise antes de avanzar (§14 del spec).
