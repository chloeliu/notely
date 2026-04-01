"""Interactive workflow mode — /workflow enters a sub-mode for managing workflows."""

from __future__ import annotations

import time

from prompt_toolkit import PromptSession
from rich.panel import Panel
from rich.prompt import Prompt

from ...config import NotelyConfig
from ...db import Database
from ._shared import console


def _workflow_mode(config: NotelyConfig) -> None:
    """Interactive workflow management mode."""
    try:
        from notely_agent.api import (
            workflow_create,
            workflow_delete,
            workflow_detail,
            workflow_edit,
            workflow_list,
            workflow_pull,
        )
    except ImportError:
        console.print(
            "[yellow]Install notely-agent for workflow support:[/yellow] "
            "pip install -e ../notely-agent"
        )
        return

    from ._completers import _WorkflowCommandCompleter

    def _list_workflows() -> list[dict]:
        workflows = workflow_list()
        if not workflows:
            console.print("[dim]No workflows found. Type 'create' to make one.[/dim]")
            return []
        for wf in workflows:
            triggers = []
            t = wf.get("trigger", {})
            if t.get("on_startup"):
                triggers.append("startup")
            if t.get("on_demand"):
                triggers.append("on-demand")
            if t.get("schedule"):
                triggers.append(f"scheduled: {t['schedule']}")
            trigger_str = f" [dim]({', '.join(triggers)})[/dim]" if triggers else ""
            console.print(f"  [cyan]{wf['name']}[/cyan] — {wf.get('description', '')}{trigger_str}")
        return workflows

    def _show_status() -> None:
        workflows = workflow_list()
        if not workflows:
            console.print("[dim]No workflows configured.[/dim]")
            return

        with Database(config.db_path) as db:
            db.initialize()
            last_runs = db.get_inbox_last_run()
            pending = db.count_inbox("pending")

        console.print("[bold]Workflow Status[/bold]\n")
        for wf in workflows:
            name = wf["name"]
            desc = wf.get("description", "")
            t = wf.get("trigger", {})

            triggers = []
            if t.get("on_startup"):
                triggers.append("startup")
            if t.get("on_demand"):
                triggers.append("on-demand")
            if t.get("schedule"):
                triggers.append(f"scheduled: {t['schedule']}")
            trigger_str = ", ".join(triggers) if triggers else "manual"

            last_run = last_runs.get(name, "")
            last_str = _format_relative_time(last_run) if last_run else "never"

            console.print(f"  [cyan]{name}[/cyan]")
            if desc:
                console.print(f"    {desc}")
            console.print(f"    Trigger:  {trigger_str}")
            console.print(f"    Last run: {last_str}")
            console.print()

        if pending:
            console.print(f"  [yellow]{pending} pending item(s) in inbox[/yellow] — review with /inbox")

    def _show_detail(name: str) -> None:
        detail = workflow_detail(name)
        if not detail:
            console.print(f"[yellow]Workflow '{name}' not found.[/yellow]")
            return

        lines = []
        if detail["description"]:
            lines.append(detail["description"])
            lines.append("")

        # --- Config ---
        src = detail["source"]
        service_str = src["service"]
        if src.get("account"):
            service_str += f" (account: {src['account']})"

        t = detail["trigger"]
        triggers = []
        if t.get("on_startup"):
            triggers.append("startup")
        if t.get("on_demand"):
            triggers.append("on-demand")
        if t.get("schedule"):
            triggers.append(f"scheduled: {t['schedule']}")

        lines.append(f"[bold]Service:[/bold]   {service_str}")
        lines.append(f"[bold]Schedule:[/bold]  {', '.join(triggers) if triggers else 'manual'}")

        # Last run from inbox
        with Database(config.db_path) as db:
            db.initialize()
            last_runs = db.get_inbox_last_run()
        last_run = last_runs.get(name, "")
        lines.append(f"[bold]Last run:[/bold]  {_format_relative_time(last_run) if last_run else 'never'}")

        # --- Pipeline ---
        lines.append("")
        lines.append("[bold]Pipeline:[/bold]")

        # Step 1: Fetch
        fetch = detail["fetch"]
        params_str = ""
        if fetch.get("params"):
            params_str = "  " + ", ".join(f"{k}={v}" for k, v in fetch["params"].items())
        lines.append(f"  1. [cyan]Fetch[/cyan]       {fetch['tool']}")
        if params_str:
            lines.append(f"                 {params_str.strip()}")

        # Step 2: Expand (if present)
        step = 2
        expand = fetch.get("expand")
        if expand:
            lines.append(f"  {step}. [cyan]Per item[/cyan]    {expand['tool']}")
            if expand.get("params"):
                exp_params = ", ".join(f"{k}={v}" for k, v in expand["params"].items())
                lines.append(f"                 {exp_params}")
            step += 1

        # Step 3: Transform or Prompt or Passthrough
        if detail.get("transform"):
            lines.append(f"  {step}. [cyan]Transform[/cyan]   field mapping:")
            for k, v in detail["transform"].items():
                lines.append(f"                 {k} = {v}")
            step += 1
        elif detail.get("prompt"):
            lines.append(f"  {step}. [cyan]AI process[/cyan]  prompt-based (see below)")
            step += 1
        else:
            lines.append(f"  {step}. [cyan]Passthrough[/cyan] raw data deposited as-is")
            step += 1

        # Step 4: Deposit to inbox
        out = detail["output"]
        dedup_parts = []
        if out.get("dedup"):
            dedup_parts.append(f"dedup by {out['dedup']}")
        if out.get("on_duplicate") and out["on_duplicate"] != "skip":
            dedup_parts.append(f"on duplicate: {out['on_duplicate']}")
        dedup_str = ", ".join(dedup_parts) if dedup_parts else "no dedup"
        lines.append(f"  {step}. [cyan]→ Inbox[/cyan]    {dedup_str}")
        lines.append(f"                 review with /inbox → file as notes")

        # --- Prompt ---
        if detail.get("prompt"):
            lines.append("")
            lines.append("[bold]Prompt:[/bold]")
            for line in detail["prompt"].strip().splitlines():
                lines.append(f"  {line}")

        console.print(Panel(
            "\n".join(lines),
            title=f"[bold]{detail['name']}[/bold]",
            border_style="cyan",
        ))

    def _delete_workflow(name: str) -> None:
        detail = workflow_detail(name)
        if not detail:
            console.print(f"[yellow]Workflow '{name}' not found.[/yellow]")
            return

        confirm = Prompt.ask(
            f"Delete workflow [cyan]{name}[/cyan]?",
            choices=["y", "n"],
            default="n",
        )
        if confirm.lower() != "y":
            console.print("[dim]Cancelled.[/dim]")
            return

        if workflow_delete(name):
            console.print(f"[green]Deleted {name}.[/green]")
            completer.invalidate()
        else:
            console.print(f"[red]Failed to delete {name}.[/red]")

    def _edit_workflow(name: str) -> None:
        result = workflow_edit(name)
        if result is None:
            console.print(f"[yellow]Workflow '{name}' not found.[/yellow]")
            return
        console.print(f"[dim]Saved {name}.[/dim]")
        completer.invalidate()

    def _schedule_workflow(name: str, schedule_expr: str) -> None:
        detail = workflow_detail(name)
        if not detail:
            console.print(f"[yellow]Workflow '{name}' not found.[/yellow]")
            return

        yaml_path = detail["yaml_path"]
        yaml_content = detail["yaml_content"]

        import yaml
        data = yaml.safe_load(yaml_content) or {}

        if not schedule_expr or schedule_expr == "off":
            # Remove schedule
            if "trigger" in data and "schedule" in data["trigger"]:
                del data["trigger"]["schedule"]
            console.print(f"[dim]Removed schedule from {name}.[/dim]")
        else:
            if "trigger" not in data:
                data["trigger"] = {}
            data["trigger"]["schedule"] = schedule_expr
            console.print(f"[green]Scheduled {name}: {schedule_expr}[/green]")

        from pathlib import Path
        with open(Path(yaml_path), "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        completer.invalidate()

    # Show commands on entry
    console.print("[bold]Workflow Commands[/bold]")
    console.print("  [cyan]create[/cyan]            Create a new workflow with AI")
    console.print("  [cyan]pull[/cyan] [dim]\\[NAME][/dim]      Run workflows, deposit results to inbox")
    console.print("  [cyan]list[/cyan]              Show available workflows")
    console.print("  [cyan]status[/cyan]            Show status and last run times")
    console.print("  [cyan]show[/cyan] [dim]NAME[/dim]         View workflow details and prompt")
    console.print("  [cyan]edit[/cyan] [dim]NAME[/dim]         Open workflow YAML in $EDITOR")
    console.print("  [cyan]delete[/cyan] [dim]NAME[/dim]       Delete a workflow")
    console.print("  [cyan]schedule[/cyan] [dim]NAME EXPR[/dim]  Set schedule (e.g. 'every 6h', or 'off')")
    console.print("  [cyan]q[/cyan]                 Exit")

    completer = _WorkflowCommandCompleter()
    session: PromptSession = PromptSession(
        completer=completer, complete_while_typing=True,
    )

    last_ctrl_c = 0.0

    while True:
        try:
            text = session.prompt("\nnotely-workflow> ").strip()
        except KeyboardInterrupt:
            now = time.monotonic()
            if now - last_ctrl_c < 2.0:
                break
            last_ctrl_c = now
            console.print("[dim]Press Ctrl+C again to exit.[/dim]")
            continue
        except EOFError:
            break

        if not text:
            continue

        low = text.lower()

        if low in ("q", "/back", "exit", "quit"):
            break

        if low == "create":
            try:
                workflow_create(config.base_dir)
                completer.invalidate()
            except KeyboardInterrupt:
                console.print("\n[yellow]Cancelled.[/yellow]")
            except Exception as e:
                console.print(f"[red]Workflow error: {e}[/red]")

        elif low in ("list", "refresh"):
            _list_workflows()
            completer.invalidate()

        elif low == "status":
            _show_status()

        elif low.startswith("show "):
            _show_detail(text[5:].strip())

        elif low.startswith("edit "):
            _edit_workflow(text[5:].strip())

        elif low.startswith("delete "):
            _delete_workflow(text[7:].strip())

        elif low.startswith("schedule "):
            parts = text[9:].strip().split(None, 1)
            if not parts:
                console.print("[yellow]Usage: schedule NAME EXPR (or 'schedule NAME off')[/yellow]")
            elif len(parts) == 1:
                # Show current schedule
                detail = workflow_detail(parts[0])
                if detail:
                    sched = detail["trigger"].get("schedule", "")
                    if sched:
                        console.print(f"  {parts[0]}: [cyan]{sched}[/cyan]")
                    else:
                        console.print(f"  {parts[0]}: [dim]no schedule[/dim]")
                else:
                    console.print(f"[yellow]Workflow '{parts[0]}' not found.[/yellow]")
            else:
                _schedule_workflow(parts[0], parts[1])

        elif low.startswith("pull"):
            import asyncio

            wf_name = text[4:].strip() or None
            if wf_name:
                console.print(f"[dim]Pulling from {wf_name}...[/dim]")
            else:
                console.print("[dim]Pulling from all workflows...[/dim]")
            try:
                new_items = asyncio.run(workflow_pull(wf_name))
                if new_items:
                    console.print(f"[cyan]{len(new_items)} new item(s) added to inbox.[/cyan]")
                else:
                    console.print("[dim]No new items.[/dim]")
            except KeyboardInterrupt:
                console.print("\n[yellow]Cancelled.[/yellow]")
            except Exception as e:
                console.print(f"[red]Pull error: {e}[/red]")

        else:
            console.print(f"[yellow]Unknown command: {text}[/yellow]")
            console.print("[dim]Type 'list' to see workflows, or 'q' to exit.[/dim]")


def _format_relative_time(iso_timestamp: str) -> str:
    """Format an ISO timestamp as a relative time string."""
    from datetime import datetime, timezone

    try:
        ts = iso_timestamp.replace("Z", "+00:00")
        if "+" in ts or ts.endswith("Z"):
            dt = datetime.fromisoformat(ts)
        else:
            dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        delta = now - dt

        seconds = int(delta.total_seconds())
        if seconds < 60:
            return "just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        if days == 1:
            return "yesterday"
        if days < 7:
            return f"{days}d ago"
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_timestamp
