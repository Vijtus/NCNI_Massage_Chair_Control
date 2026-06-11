# Basic Troubleshooting

Short checklist for the clinic. If something here does not solve the
problem, contact NCNI support: **kontakt@ncni.pl** · **+48 600 608 333**.

## Nothing happens when I click the launcher

* The first time on a Mac, `START_MACOS.command` may need
  *right-click → Open* once because it is unsigned.
* On Linux, the file may need to be marked executable in your file
  manager (or run `chmod +x START_LINUX.sh` once in a terminal).
* Run the **INSTALL** launcher once before the first **START**.

## The terminal window opens but the browser does not

1. Read the line that starts with `Panel on this computer:`.
2. Open that address (looks like `http://127.0.0.1:8080`) in your
   browser by hand.
3. If you usually use a different browser, set it as your default and
   try again.

## My phone or tablet cannot open the panel

* It **must be on the same Wi-Fi/LAN** as the computer running the
  panel.
* On the panel, tap **Sieć** (top-right corner). The popup shows the
  LAN address, IP, port, and QR. Copy or scan that.
* Allow Python through the computer's firewall when asked.

## "Port already in use"

Another instance is already running, or another program took the port.

```text
START_LINUX.sh --port 8081
```

(Adjust for the corresponding `.bat` / `.command` launcher.)

## The chair does not respond

* Check the USB cable between the computer and the controller.
* Make sure the chair is powered on at its mains switch.
* Run the verifier:

  ```text
  python3 tools/verify_installation.py
  ```

* Look at `~/.cache/ncni_massage_chair/log/bridge.log` (developer log).

## QR code missing or "qrcode library not installed"

Run the **INSTALL** launcher for your OS again. The QR feature is
optional; the panel works without it.

## Polish labels look squeezed

* Use **landscape orientation**.
* Make the browser window full-screen (`F11`).
* Re-open the panel in your browser tab.

## I just need to stop the panel

Press `Ctrl+C` in the terminal window. Or close the terminal window.
