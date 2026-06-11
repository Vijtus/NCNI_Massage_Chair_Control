# Safety

This bridge drives a real massage chair with motors, recliner actuators, and AC-mains heaters. Do not run it unattended.

## Operating Rules

- Do not auto-deploy this repository or push a branch to an environment that auto-deploys.
- Do not assume the chair is unplugged.
- Do not change command bytes, frame offsets, or zone masks without a captured UART log or explicit approval.
- Keep the chair's 33-byte UART status frame as the only source of truth. Python is a cache; the browser is a view.
- The "diode check" is the UART status frame, not GPIO reads.
- Do not replace the single-byte USB listen controls with whole-line controls unless live serial capture proves the replacement survives SoftwareSerial listening.

## Binding And LAN

The bridge now binds to `0.0.0.0` by default so the panel is reachable from
other devices on the same trusted LAN. Startup prints the selected LAN URL and
QR code; it should not print `0.0.0.0` as the address to open.

Use local-only mode for developer work that should not be reachable from other
devices:

```bash
python3 app.py --local
```

Run default LAN mode only on a trusted local network with the chair supervised.

## Failed vs Unverified

Each command gets a sequence number. Firmware reports `ACK` when it starts holding the byte and `DONE` after the post-press gap.

If the chair frame does not confirm the optimistic model immediately after `DONE`, the bridge waits a bounded 2.5 s verification-settle window because the chair frame can lag the emitted byte. After settle the bridge reaches one of three terminal states:

1. **Completed.** Confirmed frame fields agree with the optimistic values. Mute clears, button keeps its new state, no chip.
2. **Failed.** Confirmed frame fields disagree. The bridge:
    - records `state.last_error`
    - appends to `state.failed_commands`
    - clears the optimistic mute
    - surrenders the model back to the current chair frame
    - flashes the failed button red briefly
3. **Unverified.** None of the command's muted fields have a confirmed frame mapping yet, so the chair frame cannot prove agreement or disagreement. The bridge:
    - appends to `state.unverified_commands`
    - clears the optimistic mute
    - surfaces a neutral chip in the UI
    - does NOT flash the button red, does NOT set `state.last_error`, does NOT auto-resend

Unverified is the honest "the chair never confirmed and we couldn't tell either way" state. It exists so press-with-no-mapping does not paint the UI red on a press that may have actually worked. As isolated capture results are added to maintenance notes or tests, fields move from unmapped to confirmed and the unverified path stops applying to those commands.

Automatic retry is reserved for future commands explicitly proven to be idempotent set-state actions, one at a time, each backed by hardware evidence.

Some buttons are frame-observed because live captures proved the old optimistic toggle model was wrong. For those, the bridge sends the button press and waits for the chair frame to update the view instead of displaying a guessed target state.
