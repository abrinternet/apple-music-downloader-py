"""Interactive UI helpers.

Replaces the Go dependencies AlecAivazis/survey (select prompts),
olekukonko/tablewriter (tables) and fatih/color.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table as RichTable

console = Console()


def render_table(
    headers: list[str],
    rows: list[list[str]],
    caption: str | None = None,
) -> None:
    table = RichTable(show_lines=False)
    for h in headers:
        table.add_column(h)
    if caption:
        table.caption = caption
    for row in rows:
        table.add_row(*[str(cell) for cell in row])
    console.print(table)


def select_option(message: str, options: list[str], page_size: int = 5) -> int | None:
    """Arrow-key single select mirroring survey.Select.

    Returns the chosen index, or None when the user cancels (Ctrl+C / Esc).
    """
    try:
        from prompt_toolkit.shortcuts import radiolist_dialog

        result = radiolist_dialog(
            title=message,
            text=message,
            values=[(i, opt) for i, opt in enumerate(options)],
        ).run()
        if result is None:
            return None
        return int(result)
    except (KeyboardInterrupt, EOFError):
        return None
