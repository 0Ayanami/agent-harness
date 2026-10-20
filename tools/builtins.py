"""Register service capabilities as tools; no application state lives here."""

from .schemas import BUILTIN_SCHEMAS


def register_builtins(registry, *, workspace, tasks, context, subagents, scheduler, team, mcp):
    def in_assignment(handler):
        def execute(**arguments):
            cwd, error = tasks.agent_cwd()
            return error or handler(cwd=cwd, **arguments)

        return execute

    handlers = {
        "bash": in_assignment(workspace.run_bash),
        "read_file": in_assignment(workspace.run_read),
        "write_file": in_assignment(workspace.run_write),
        "edit_file": in_assignment(workspace.run_edit),
        "glob": in_assignment(workspace.run_glob),
        "todo_write": tasks.run_todo_write,
        "task": subagents.run,
        "load_skill": context.load_skill,
        "compact": lambda **_: "Compaction requested",
        "create_task": tasks.run_create_task,
        "update_task": tasks.run_update_task,
        "list_tasks": tasks.run_list_tasks,
        "get_task": tasks.run_get_task,
        "claim_task": tasks.run_claim_task,
        "complete_task": tasks.run_complete_task,
        "schedule_cron": scheduler.run_schedule_cron,
        "list_crons": scheduler.run_list_crons,
        "cancel_cron": scheduler.run_cancel_cron,
        "spawn_teammate": team.run_spawn_teammate,
        "list_teammates": team.run_list_teammates,
        "send_message": team.run_send_message,
        "request_shutdown": team.run_request_shutdown,
        "request_plan": team.run_request_plan,
        "review_plan": team.run_review_plan,
        "create_worktree": tasks.create_worktree,
        "connect_mcp": mcp.connect_mcp,
    }
    for definition in BUILTIN_SCHEMAS:
        registry.register(definition, handlers[definition["name"]])
