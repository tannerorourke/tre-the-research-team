---
name: tre
description: "Operate TRE research runs from this session through the plugin's tre MCP server: register the repository, start a research workflow (literature, design, planning, execution, analysis, study), watch it to its exit line, answer its human gates, and report the outcome. Use when the user asks to run a TRE workflow or study, resume, answer a gate, or keep watch on a tre process, mentions of the research team, research agents, or \"tre\""
---

# Operate TRE

When this loop is done, every run you started has ended on its exit line, each gate it hit was answered by the user, and the outcome is written in the project's `.claude/memory/status.md`. The server does the work; this session starts runs, watches them, and carries gates to the user. A run's truth is on disk under `<repo>/.tre/runs/<run_id>/`, so a run outlives this session, the server, and any watcher.

Every run ends on one line its launcher writes:

```
[tre] <run_id> exit <code> status=<status> gate=<gate or ->
```

Match it with `^\[tre\] \S+ exit \d+ status=\S+ gate=\S+$`. Exit 0 is `completed`, 3 is `waiting_gate`, 1 is anything else. Treat silence from a watcher as unknown, never as success.

## Launch

1. Confirm the server: read the MCP resource `tre://repos`. On a connection error the plugin's `tre serve --stdio` did not start; tell the user to run `uvx --from <plugin root> tre serve --stdio` in a terminal and read its stderr, and stop until `tre://repos` reads. A daemon started with `tre serve` is optional; the stdio server proxies to it when one is listening.
2. Register the repository: call `repo_register` with the absolute root (`git rev-parse --show-toplevel`). It is idempotent.
3. Start the run: call `run_start` with `repo`, `workflow`, and `input`. Pass `answers` only when the user has pre-answered a gate by name. Keep the returned `run_id`, `repo`, and `wait` command.
4. Write the resume recipe into the project's `.claude/memory/status.md` before anything else, so a session that dies mid-run leaves a record:

   ```markdown
   ## TRE run <run_id>
   - Repository: <repo>
   - Wait: tre wait <run_id> -p <repo>
   - Next step: <what to do on completed, or "none">
   - Fail-safe cron: <cron id>
   ```

5. Arm the watch and the fail-safe cron below. Leave the run unattended only after the Monitor's first event, `[watch] armed`, has arrived.

## Watch

Arm one persistent Monitor per run:

```bash
echo "[watch] armed <run_id>"; tre wait <run_id> -p <repo> --poll 10
```

The command blocks while `meta.json` reads `running`, prints the exit line, and exits; the exit line ends the watch. Use the `tre` on `PATH`, or the absolute path to the one the server runs. `tre wait` reads only the disk, so it keeps working through a server restart.

A worker that dies without writing its status leaves `meta.json` at `running`. `tre wait` probes the worker's pid from `worker.json` and, after two polls with it gone, prints the exit line with `status=died` and exits 1 without writing anything; `run_status` is what records `died` in `meta.json`, and the fail-safe cron makes that call on a schedule.

## Fail-safe cron

The Monitor can die silently (VM sleep, Monitor auto-stop, session restart). Arm a second channel that shares nothing with it: a session cron (`CronCreate`, every 30 minutes on an off-minute such as `13,43 * * * *`) whose prompt calls `run_status` and acts on the table below. Write the prompt self-contained, because it fires with no other context:

```text
TRE fail-safe for run <run_id> in <repo>. Call the tre MCP tool run_status with run_id=<run_id>.
Wait command: tre wait <run_id> -p <repo> --poll 10. Exit-line regex: ^\[tre\] \S+ exit \d+ status=\S+ gate=\S+$.
Next step on completed: <next step or "none">. Act on 'status' with this table: <paste the state-to-action table>.
If run_status fails to connect, PushNotification that the server is down; the run keeps going on disk.
```

Session crons expire after 7 days and die with the session; re-arm after any restart from the resume recipe. Delete the cron when the run is closed out.

## State to action

Read `status` from `run_status`, or from the exit line when the Monitor ends.

| Status | Action |
| --- | --- |
| `running` | Nothing if the Monitor for this run is live (`TaskList`); otherwise re-arm it and note it. Reply with one line at most. |
| `queued` | The supervisor's concurrency cap is full and the run starts when a slot frees. `tre wait` keeps polling through `queued`, so the Monitor stays armed. |
| `waiting_gate` | Read `gate` and `gate_message` from `run_status`. Put the message to the user verbatim with `AskUserQuestion`, offering `yes` to approve and a free-text note to decline. Call `run_answer` with `run_id`, `gate`, and the user's answer as `value`, then re-arm the Monitor on the same wait command. Answer only with the user's words; a gate is theirs unless they told you to run unattended. |
| `completed` | Run the queued next step, else close out. `PushNotification` with the outcome. |
| `failed` | Read the tail of `tre://runs/<run_id>/events?tail=50` and report the failing node and its error. `PushNotification`. Relaunch only on the user's go. |
| `died` | Call `run_status` and report `alive`, the last events from `tre://runs/<run_id>/events?tail=50`, and the node in flight. `PushNotification`. Never relaunch blindly: the supervisor already relaunches a `died` run once from its checkpoint when it starts. |
| `cancelled` | Report who or what cancelled it if the events show it, then close out. |

## Close out

When the run ends on `completed`, `failed`, `died`, or `cancelled`: `TaskStop` the run's Monitor, `CronDelete` its fail-safe cron (`CronList` shows it), and replace the resume recipe in `.claude/memory/status.md` with the outcome, the run id, and the notebook entries the run wrote. The run is closed out when no Monitor or cron for it remains.

## Other tools

- `run_list` and `tre://gates` show every run and every run waiting at a gate across registered repositories; read `tre://gates` when the user asks what needs them.
- `run_cancel` stops a run's whole process group; call it only on the user's word.
- `costs` and `standup` summarize spend and lab state for a repository.
- The lab tools (`paper_*`, `notebook_*`, `kg_*`, `repo_map`, `git_read`) take a `repo` argument naming a registered root.
- The `research-scientist` and `tech-lead` skills carry the two user-facing charters, for when the user wants a judgment in that role.
