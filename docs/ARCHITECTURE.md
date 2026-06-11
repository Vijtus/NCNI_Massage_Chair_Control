# Architecture

## UART Flow

Browser commands are posted to Python as command names. Python assigns a 16-bit sequence number and writes:

```text
<seq> <command-name>\n
```

Firmware queues the press in an 8-slot static ring buffer. Every 100 ms it emits one byte to the chair UART:

```text
IDLE -> HOLDING(code for 3 ticks) -> GAPPING(0x00 for 3 ticks) -> IDLE
```

Firmware prints `ACK seq=N code=0xXX` when holding starts and `DONE seq=N code=0xXX` when the gap ends. Firmware prints `NACK seq=N code=0xXX error=...` for queued-protocol rejects. Python tracks `in_flight`, `acked`, `verifying`, and `completed` commands. ACK timeout is 2 s. DONE timeout is 5 s.

Python accepts only one pending browser command at a time. While a command is queued, in flight, ACKed, or in the verification-settle window, `/api/command` rejects additional presses and the UI marks buttons blocked. This deliberately prevents long toggle backlogs and AVR USB RX line corruption while the Nano is also streaming chair-frame data.

The Python-to-firmware USB control path uses immediate single-byte controls for chair-side UART listening: `!` means listen off and `~` means listen on. Python sends redundant `!` bytes before a command line because live capture showed full text controls such as `listen off` can be corrupted while SoftwareSerial is listening to the chair.

Firmware emits compact status frames as `FRAME <66 hex chars>`. The bridge still accepts the older `RX: 0x..` debug format, but compact frames are the normal path.

`DONE` means the firmware finished emitting the button press, not that the chair status frame has reacted. If frame values do not agree at `DONE`, Python holds the mute and waits a bounded 2.5 s verification-settle window before retry/surrender decisions.

Current chair commands are treated as non-idempotent button presses. They are verified closed-loop, but they are not automatically resent on a disagreeing frame because a retry can double-toggle a command that actually reached the chair before the status frame caught up. Retry support remains in the bridge only for commands explicitly added to `RETRY_SAFE_COMMANDS` after hardware evidence proves they are idempotent set-state actions.

## Mute Window

When Python applies an optimistic command, it records the touched model fields and mutes those fields for 0.75 s. Incoming chair frames still parse during the mute, but muted fields are not overwritten by stale frames.

Mute clears on deadline, on verified `DONE`, on verified settle-window completion, or on failed-command surrender.

## Verify And Retry

On `DONE`, Python compares the frame-derived values for the command's muted fields with the optimistic values captured at click time. Only confirmed frame mappings are compared. Verification has three terminal states:

- **completed** — at least one confirmed field was checked and all checked fields agreed.
- **failed** — at least one confirmed field was checked and at least one disagreed. Bridge logs `state.last_error`, appends to `state.failed_commands`, clears mute, surrenders the model back to the frame, and flashes the button red.
- **unverified** — the command had muted fields but none of them have a confirmed frame mapping yet (`checked == 0`). Bridge appends to `state.unverified_commands`, clears mute, and surfaces a neutral chip. It does not set `last_error`, does not flash the button red, and does not auto-resend.

Commands whose semantics are not predictable from current captures are frame-observed: the bridge sends the press and waits for fresh chair frames to update the UI instead of guessing a target state. Frame-observed commands carry empty muted fields, so they verify trivially as completed (zero checks, zero disagreements) and never appear in `unverified_commands`.

## Confirmed Frame Mappings

| Model field | Frame source | Check |
| --- | --- | --- |
| `power_on` | payload bytes 2..32 | not all zero |
| `shoulders_on` | `frame[21] & 0x01` | direct |
| `forearms_on` | `frame[21] & 0x02` | direct |
| `legs_on` | `frame[21] & 0x04` | direct |
| `heat_on` | `(frame[23] & 0x0C) != 0` | direct |
| `mode == auto` | `b3 == 0x04 && b4 == 0x02` | direct |
| `mode == manual` | `b4 == 0x0C || b4 == 0x0E` | direct |
| `auto_profile` | `frame[17..20]` known signatures | tolerant |

The reverse-engineering `b3..b6` view maps to live 33-byte positions `[3, 4, 11, 12]`.
