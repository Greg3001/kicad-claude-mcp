"""Phase 15 — panelize boards into a grid for batch fab.

Tools:
    panelize_board_grid    — duplicate the active PCB rows × cols, save as panel.kicad_pcb
"""

from __future__ import annotations

import logging
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import panelization, sch_editor, sch_io
from kicad_claude.adapters import project_settings as ps

logger = logging.getLogger("kicad-claude.tools.panelization")


def register(mcp) -> None:
    @mcp.tool()
    def panelize_board_grid(
        rows: int,
        cols: int,
        h_gap_mm: float = 2.0,
        v_gap_mm: float = 2.0,
        mouse_bites: bool = True,
        mouse_bite_drill_mm: float = 0.5,
        mouse_bite_spacing_mm: float = 1.0,
        output_filename: str = "panel.kicad_pcb",
        register_in_project: bool = True,
        switch_to_panel: bool = False,
    ) -> dict:
        """Panelize the active PCB by duplicating it into a `rows × cols` grid.

        - Each cell is a verbatim copy (footprints, tracks, zones, silk).
          Refs are suffixed with `_R{r}C{c}` so KiCAD's DRC sees unique names.
        - Mouse bites = small Edge.Cuts circles between cells; the resulting
          panel snaps apart along the perforation lines after fab.
        - The new PCB is saved alongside the source as `output_filename`. If
          `register_in_project=True`, it's added to `<project>.kicad_pro`'s
          `boards` list. `switch_to_panel=True` makes it the active board.

        Note: zones in cells get their `(filled_polygon ...)` cache stripped
        (the outline polygon is preserved). Run `run_drc(refill_zones=True)`
        on the panel to recompute fills, then re-export gerbers.
        """
        proj = state.get_active()
        source_pcb = state.get_active_board_path()
        source_tree = sch_io.parse_file(source_pcb)

        result = panelization.panelize_grid(
            source_tree,
            rows=rows, cols=cols,
            h_gap_mm=h_gap_mm, v_gap_mm=v_gap_mm,
            mouse_bites=mouse_bites,
            mouse_bite_drill_mm=mouse_bite_drill_mm,
            mouse_bite_spacing_mm=mouse_bite_spacing_mm,
        )

        out_path = proj.path / output_filename
        if out_path.is_file():
            backup = sch_editor.backup_file(out_path)
        else:
            backup = None
        sch_io.write_file(out_path, result["tree"])

        if register_in_project:
            pro = ps.load_pro(proj.pro_path)
            boards = pro.setdefault("boards", [])
            if output_filename not in boards:
                boards.append(output_filename)
            ps.save_pro(proj.pro_path, pro)

        if switch_to_panel:
            state.set_active_board(output_filename)

        return {
            "panel_path": str(out_path),
            "rows": rows,
            "cols": cols,
            "cell_count": result["cell_count"],
            "cell_size_mm": result["cell_size_mm"],
            "panel_size_mm": result["outline_size_mm"],
            "mouse_bites": mouse_bites,
            "registered_in_project": register_in_project,
            "active_board_switched": switch_to_panel,
            "backup": str(backup) if backup else None,
        }
